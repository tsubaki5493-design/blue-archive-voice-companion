"""
MomoTalk Memory Note & Shared Sensei Profile Dialog
---------------------------------------------------
Clean MomoTalk-themed modal (`tk.Toplevel`) giving users transparent control over:
  - Tab 0: Per-student structured memories (Confirmed with search/filter & inline detail editor,
           Pending STT-review hold, Open Threads), 1-click snapshot rollback, manual add/edit/delete,
           and safe track reset
  - Tab 1: Shared Sensei Profile (known across all students)
  - Tab 2: JSON Backup / Restore & isolated storage folder access
"""

from __future__ import annotations

import datetime
import os
import re
import subprocess
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from src.controller.memory_manager import (
    MEMORY_CATEGORIES,
    MEMORY_CATEGORY_LABELS,
    MEMORY_TRACK_LABELS,
    VALID_MEMORY_TRACKS,
    MemoryManager,
    _safe_int,
    _validate_student_id,
    format_timestamp_safe,
)
from src.utils.app_paths import redact_secrets
from src.utils.ui_scroll import (
    ScrollableFrame,
    bind_responsive_wrap,
    fit_window_to_screen,
    make_pinned_footer,
)

_CAT_LABEL_TO_CODE: dict[str, str] = {v: k for k, v in MEMORY_CATEGORY_LABELS.items()}
_FILTER_ALL_LABEL = "전체 카테고리"
_SAFE_FILENAME_TOKEN_RE = re.compile(r"[^A-Za-z0-9_-]+")
_MAX_MEMORY_CHARS = 120


def _safe_backup_filename(base_cid: str, stamp: str, single_student: bool) -> str:
    """Build a strictly sanitized default JSON backup filename (zero traversal/separator risk)."""
    clean_stamp = _SAFE_FILENAME_TOKEN_RE.sub("", str(stamp or ""))[:20] or "backup"
    if single_student and base_cid:
        token = _SAFE_FILENAME_TOKEN_RE.sub("_", str(base_cid)).strip("_")[:48]
        if token:
            return f"momotalk_memory_{token}_{clean_stamp}.json"
    return f"momotalk_memory_all_{clean_stamp}.json"


def _validate_open_folder_target(folder: Path, expected_dir: Path) -> Path:
    """Verify folder target is an existing non-symlink directory matching `expected_dir`."""
    raw_str = str(folder)
    if "\x00" in raw_str:
        raise ValueError("Invalid folder path")
    if folder.is_symlink():
        raise ValueError("Refusing to open symlinked memory directory")
    resolved = folder.resolve()
    expected = expected_dir.resolve()
    if resolved != expected or not resolved.is_dir() or not resolved.is_absolute():
        raise ValueError("Memory folder path validation failed")
    if os.name == "nt" and str(resolved).startswith(("/", "-")):
        raise ValueError("Invalid Windows directory path")
    return resolved


class MemoryNoteDialog(tk.Toplevel):
    """Modal dialog for viewing/editing student memories, Sensei profile, and JSON backups."""

    def __init__(
        self,
        parent: tk.Misc,
        memory_mgr: MemoryManager,
        base_cid: str,
        student_name_kr: str,
        track: str = "official",
        on_changed: Callable[[], None] | None = None,
        initial_tab: int = 0,
    ) -> None:
        super().__init__(parent)
        self.memory_mgr = memory_mgr
        raw_cid = (base_cid or "").strip()
        if raw_cid:
            try:
                self.base_cid = _validate_student_id(raw_cid)
            except ValueError:
                self.base_cid = ""
        else:
            self.base_cid = ""
        self.student_name_kr = (student_name_kr or self.base_cid or "학생").strip()
        self.active_track = track if track in ("official", "fanon", "custom") else "official"
        self.on_changed = on_changed

        self._all_confirmed_items: list[dict[str, Any]] = []
        self._confirmed_ids: list[str] = []
        self._pending_items: list[dict[str, Any]] = []
        self._pending_ids: list[str] = []
        self._selected_confirmed_id: str = ""
        self._suspend_search_trace: bool = False
        self._destroyed: bool = False
        self._search_trace_token: str | None = None
        self._sp_prefs_trace_token: str | None = None

        self.title(f"MomoTalk Live 기억 노트 — {self.student_name_kr}")
        self.configure(bg="#F6F8FB")
        fit_window_to_screen(
            self,
            preferred_w=820,
            preferred_h=700,
            min_w=600,
            min_h=460,
            center_on=parent,
        )
        self.transient(parent)
        try:
            self.grab_set()
        except tk.TclError:
            pass

        self._build_ui(initial_tab=max(0, min(2, int(initial_tab))))
        self._refresh_student_tab()
        self._load_sensei_profile_into_ui()
        self._refresh_backup_summary()

        self.bind("<Escape>", self._on_escape_key)
        self.bind("<Control-f>", self._on_ctrl_f)
        self.bind("<Control-F>", self._on_ctrl_f)
        self.bind("<Control-s>", self._on_ctrl_s)
        self.bind("<Control-S>", self._on_ctrl_s)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def _notify_changed(self) -> None:
        if callable(self.on_changed):
            try:
                self.on_changed()
            except Exception:
                pass

    def _build_ui(self, initial_tab: int = 0) -> None:
        # 1. Pinned Top MomoTalk Coral Header
        hdr = tk.Frame(self, bg="#FB7390", padx=16, pady=10)
        hdr.pack(side="top", fill="x")
        tk.Label(
            hdr,
            text="🧠 기억 노트 & 선생님 프로필",
            bg="#FB7390",
            fg="#FFFFFF",
            font=("Malgun Gothic", 12, "bold"),
        ).pack(side="left")
        tk.Label(
            hdr,
            text=self.student_name_kr if self.base_cid else "전역 관리",
            bg="#E45C7A",
            fg="#FFFFFF",
            font=("Malgun Gothic", 9, "bold"),
            padx=10,
            pady=2,
        ).pack(side="right")

        # 2. Pinned Bottom Footer (packed BEFORE body; right buttons packed BEFORE expanding status label)
        footer = make_pinned_footer(
            self,
            bg="#F6F8FB",
            border_color="#CBD5E1",
            padx=14,
            pady=(8, 10),
            separator=True,
        )
        tk.Button(
            footer,
            text="닫기",
            command=self.destroy,
            bg="#334155",
            fg="#FFFFFF",
            activebackground="#1E293B",
            activeforeground="#FFFFFF",
            font=("Malgun Gothic", 9, "bold"),
            relief="flat",
            padx=16,
            pady=5,
            cursor="hand2",
        ).pack(side="right")

        tk.Label(
            footer,
            text="Ctrl+F 검색 · Ctrl+S 저장 · Esc 닫기",
            bg="#F6F8FB",
            fg="#64748B",
            font=("Malgun Gothic", 8),
        ).pack(side="right", padx=(0, 10))

        self.lbl_dialog_status = tk.Label(
            footer,
            text="",
            bg="#F6F8FB",
            fg="#0F766E",
            font=("Malgun Gothic", 9, "bold"),
            anchor="w",
            justify="left",
        )
        self.lbl_dialog_status.pack(side="left", fill="x", expand=True, padx=(0, 8))
        bind_responsive_wrap(self.lbl_dialog_status, container=footer, pad=260, min_wrap=160)

        # 3. Main Body with Scrollable Tabs
        body = tk.Frame(self, bg="#F6F8FB", padx=12, pady=8)
        body.pack(side="top", fill="both", expand=True)

        self.notebook = ttk.Notebook(body)
        self.notebook.pack(fill="both", expand=True)

        self.tab_student_scroll = ScrollableFrame(
            self.notebook,
            bg="#FFFFFF",
            padx=14,
            pady=12,
            bottom_pad=28,
            auto_scroll_on_focus=True,
        )
        self.tab_sensei_scroll = ScrollableFrame(
            self.notebook,
            bg="#FFFFFF",
            padx=16,
            pady=14,
            bottom_pad=28,
            auto_scroll_on_focus=True,
        )
        self.tab_backup_scroll = ScrollableFrame(
            self.notebook,
            bg="#FFFFFF",
            padx=16,
            pady=14,
            bottom_pad=28,
            auto_scroll_on_focus=True,
        )

        self.tab_student = self.tab_student_scroll.inner
        self.tab_sensei = self.tab_sensei_scroll.inner
        self.tab_backup = self.tab_backup_scroll.inner

        self.notebook.add(self.tab_student_scroll, text=f"🧠 {self.student_name_kr} 기억")
        self.notebook.add(self.tab_sensei_scroll, text="👤 공통 선생님 프로필")
        self.notebook.add(self.tab_backup_scroll, text="💾 백업 · 복원")

        self._build_student_tab()
        self._build_sensei_tab()
        self._build_backup_tab()

        if not self.base_cid and initial_tab == 0:
            initial_tab = 1
        self.notebook.select(initial_tab)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

    def _on_tab_changed(self, _event: Any = None) -> None:
        try:
            idx = int(self.notebook.index(self.notebook.select()))
        except Exception:
            return
        scrolls = (self.tab_student_scroll, self.tab_sensei_scroll, self.tab_backup_scroll)
        if 0 <= idx < len(scrolls):
            scrolls[idx].refresh_scrollregion(defer=False)
        if idx == 2:
            self._refresh_backup_summary()

    def destroy(self) -> None:
        self._destroyed = True
        if self._search_trace_token and hasattr(self, "search_var"):
            try:
                self.search_var.trace_remove("write", self._search_trace_token)
            except Exception:
                pass
            self._search_trace_token = None
        if self._sp_prefs_trace_token and hasattr(self, "sp_prefs_var"):
            try:
                self.sp_prefs_var.trace_remove("write", self._sp_prefs_trace_token)
            except Exception:
                pass
            self._sp_prefs_trace_token = None
        super().destroy()

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
        if focused is getattr(self, "ent_search", None) and self.search_var.get().strip():
            self._clear_search_filter()
            return "break"
        self.destroy()
        return "break"

    def _on_ctrl_f(self, _event: Any = None) -> str | None:
        if not self.base_cid or not hasattr(self, "ent_search"):
            return None
        try:
            self.notebook.select(0)
            self.tab_student_scroll.scroll_to_widget(self.ent_search)
            self.ent_search.focus_set()
            self.ent_search.selection_range(0, tk.END)
            return "break"
        except Exception:
            return None

    def _on_ctrl_s(self, _event: Any = None) -> str | None:
        try:
            tab_idx = int(self.notebook.index(self.notebook.select()))
        except Exception:
            return None
        if tab_idx == 1:
            self._on_save_sensei_profile()
            return "break"
        if tab_idx == 0:
            try:
                focused = self.focus_get()
            except Exception:
                focused = None
            if focused is getattr(self, "txt_threads", None):
                self._on_save_threads()
                return "break"
            if self._selected_confirmed_id:
                self._on_save_detail_edit()
                return "break"
            self._on_save_threads()
            return "break"
        return None

    def _set_status(self, msg: str, color: str = "#0F766E") -> None:
        self.lbl_dialog_status.configure(text=msg, fg=color)

    # ------------------------------------------------------------------
    # Tab 0: Student Memory Note
    # ------------------------------------------------------------------

    def _build_student_tab(self) -> None:
        top = tk.Frame(self.tab_student, bg="#FFFFFF")
        top.pack(fill="x", pady=(0, 8))

        # Pack right track pills BEFORE expanding meta label so pills never squeeze at min_w
        track_pill = tk.Frame(top, bg="#F1F5F9", padx=3, pady=3)
        track_pill.pack(side="right", padx=(8, 0))
        self._track_btns: dict[str, tk.Button] = {}
        for tr_code in VALID_MEMORY_TRACKS:
            btn = tk.Button(
                track_pill,
                text=MEMORY_TRACK_LABELS[tr_code],
                command=lambda c=tr_code: self._switch_track(c),
                font=("Malgun Gothic", 8, "bold"),
                relief="flat",
                bd=0,
                padx=7,
                pady=2,
                cursor="hand2",
            )
            btn.pack(side="left", padx=2)
            self._track_btns[tr_code] = btn

        self.lbl_student_meta = tk.Label(
            top,
            text="",
            bg="#FFFFFF",
            fg="#334155",
            font=("Malgun Gothic", 9, "bold"),
            anchor="w",
            justify="left",
        )
        self.lbl_student_meta.pack(side="left", fill="x", expand=True)
        bind_responsive_wrap(self.lbl_student_meta, container=top, pad=260, min_wrap=160)

        # Confirmed memories header
        cf_hdr = tk.Frame(self.tab_student, bg="#FFFFFF")
        cf_hdr.pack(fill="x", pady=(4, 4))
        self.lbl_confirmed_title = tk.Label(
            cf_hdr,
            text="✅ 확정 장기 기억",
            bg="#FFFFFF",
            fg="#1E293B",
            font=("Malgun Gothic", 10, "bold"),
        )
        self.lbl_confirmed_title.pack(side="left")
        tk.Button(
            cf_hdr,
            text="🗑️ 삭제",
            command=self._on_delete_confirmed,
            bg="#FEE2E2",
            fg="#B91C1C",
            activebackground="#FECACA",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=8,
            pady=2,
            cursor="hand2",
        ).pack(side="right", padx=(4, 0))
        tk.Button(
            cf_hdr,
            text="✏️ 수정",
            command=self._on_edit_confirmed,
            bg="#E0F2FE",
            fg="#0369A1",
            activebackground="#BAE6FD",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=8,
            pady=2,
            cursor="hand2",
        ).pack(side="right")

        # Search & category filter bar
        filter_bar = tk.Frame(
            self.tab_student,
            bg="#F8FAFC",
            highlightbackground="#E2E8F0",
            highlightthickness=1,
            padx=8,
            pady=5,
        )
        filter_bar.pack(fill="x", pady=(0, 6))
        tk.Label(
            filter_bar,
            text="🔍",
            bg="#F8FAFC",
            fg="#64748B",
            font=("Malgun Gothic", 9),
        ).pack(side="left", padx=(0, 4))
        self.search_var = tk.StringVar(master=self)
        self._search_trace_token = self.search_var.trace_add("write", self._on_search_changed)
        self.ent_search = tk.Entry(
            filter_bar,
            textvariable=self.search_var,
            font=("Malgun Gothic", 9),
            bg="#FFFFFF",
            fg="#0F172A",
            relief="solid",
            bd=1,
        )
        self.ent_search.pack(side="left", fill="x", expand=True, padx=(0, 6))

        self.filter_cat_var = tk.StringVar(value=_FILTER_ALL_LABEL)
        self.cmb_filter_cat = ttk.Combobox(
            filter_bar,
            textvariable=self.filter_cat_var,
            values=[_FILTER_ALL_LABEL] + [MEMORY_CATEGORY_LABELS[c] for c in MEMORY_CATEGORIES],
            state="readonly",
            width=13,
        )
        self.cmb_filter_cat.pack(side="left", padx=(0, 6))
        self.cmb_filter_cat.bind("<<ComboboxSelected>>", lambda _e: self._apply_confirmed_filter())

        self.btn_clear_filter = tk.Button(
            filter_bar,
            text="초기화",
            command=self._clear_search_filter,
            bg="#E2E8F0",
            fg="#334155",
            activebackground="#CBD5E1",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=7,
            pady=1,
            cursor="hand2",
        )
        self.btn_clear_filter.pack(side="right")

        # Confirmed Listbox + vertical Scrollbar
        cf_wrap = tk.Frame(self.tab_student, bg="#FFFFFF", highlightbackground="#CBD5E1", highlightthickness=1)
        cf_wrap.pack(fill="x")
        self.lst_confirmed = tk.Listbox(
            cf_wrap,
            height=7,
            font=("Malgun Gothic", 9),
            bg="#FFFFFF",
            fg="#0F172A",
            selectbackground="#FFE4EC",
            selectforeground="#9D174D",
            relief="flat",
            bd=0,
            activestyle="none",
            exportselection=False,
        )
        cf_sb = ttk.Scrollbar(cf_wrap, orient="vertical", command=self.lst_confirmed.yview)
        self.lst_confirmed.configure(yscrollcommand=cf_sb.set)
        cf_sb.pack(side="right", fill="y")
        self.lst_confirmed.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        self.lst_confirmed.bind("<<ListboxSelect>>", self._on_confirmed_select)
        self.lst_confirmed.bind("<Double-Button-1>", lambda _e: self._on_edit_confirmed())
        self.lst_confirmed.bind("<Return>", lambda _e: self._on_edit_confirmed())
        self.lst_confirmed.bind("<Delete>", lambda _e: self._on_delete_confirmed())

        # Inline Detail Viewer & Editor Card (prevents horizontal clipping of long memories!)
        self.detail_card = tk.Frame(
            self.tab_student,
            bg="#F8FAFC",
            highlightbackground="#E2E8F0",
            highlightthickness=1,
            padx=10,
            pady=8,
        )
        self.detail_card.pack(fill="x", pady=(6, 8))

        det_top = tk.Frame(self.detail_card, bg="#F8FAFC")
        det_top.pack(fill="x", pady=(0, 4))
        self.lbl_detail_chars = tk.Label(
            det_top,
            text=f"0 / {_MAX_MEMORY_CHARS}자",
            bg="#F8FAFC",
            fg="#64748B",
            font=("Malgun Gothic", 8),
        )
        self.lbl_detail_chars.pack(side="right", padx=(8, 0))
        self.lbl_detail_meta = tk.Label(
            det_top,
            text="📝 목록에서 기억을 클릭하면 전체 내용 확인 및 바로 수정할 수 있습니다.",
            bg="#F8FAFC",
            fg="#475569",
            font=("Malgun Gothic", 8, "bold"),
            anchor="w",
            justify="left",
        )
        self.lbl_detail_meta.pack(side="left", fill="x", expand=True)
        bind_responsive_wrap(self.lbl_detail_meta, container=det_top, pad=110, min_wrap=180)

        det_txt_wrap = tk.Frame(self.detail_card, bg="#FFFFFF", highlightbackground="#CBD5E1", highlightthickness=1)
        det_txt_wrap.pack(fill="x", pady=(0, 6))
        self.txt_detail = tk.Text(
            det_txt_wrap,
            height=3,
            font=("Malgun Gothic", 9),
            bg="#FFFFFF",
            fg="#0F172A",
            relief="flat",
            bd=0,
            wrap="word",
            padx=6,
            pady=4,
            state="disabled",
        )
        det_sb = ttk.Scrollbar(det_txt_wrap, orient="vertical", command=self.txt_detail.yview)
        self.txt_detail.configure(yscrollcommand=det_sb.set)
        det_sb.pack(side="right", fill="y")
        self.txt_detail.pack(side="left", fill="both", expand=True)
        self.txt_detail.bind("<KeyRelease>", self._update_detail_char_count)
        self.txt_detail.bind("<Control-Return>", lambda _e: (self._on_save_detail_edit(), "break")[1])

        det_btns = tk.Frame(self.detail_card, bg="#F8FAFC")
        det_btns.pack(fill="x")
        tk.Label(
            det_btns,
            text="분류:",
            bg="#F8FAFC",
            fg="#475569",
            font=("Malgun Gothic", 8, "bold"),
        ).pack(side="left", padx=(0, 4))
        self.detail_cat_var = tk.StringVar(value=MEMORY_CATEGORY_LABELS["fact"])
        self.cmb_detail_cat = ttk.Combobox(
            det_btns,
            textvariable=self.detail_cat_var,
            values=[MEMORY_CATEGORY_LABELS[c] for c in MEMORY_CATEGORIES],
            state="disabled",
            width=12,
        )
        self.cmb_detail_cat.pack(side="left")
        self.btn_save_detail = tk.Button(
            det_btns,
            text="💾 수정 저장",
            command=self._on_save_detail_edit,
            state="disabled",
            bg="#0EA5E9",
            fg="#FFFFFF",
            activebackground="#0284C7",
            activeforeground="#FFFFFF",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=10,
            pady=2,
            cursor="hand2",
        )
        self.btn_save_detail.pack(side="right")
        self.btn_revert_detail = tk.Button(
            det_btns,
            text="되돌리기",
            command=self._revert_detail_editor,
            state="disabled",
            bg="#E2E8F0",
            fg="#334155",
            activebackground="#CBD5E1",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=8,
            pady=2,
            cursor="hand2",
        )
        self.btn_revert_detail.pack(side="right", padx=(0, 6))

        # Quick manual add bar
        add_lbl = tk.Label(
            self.tab_student,
            text="➕ 새 장기 기억 직접 추가 (Enter로 바로 등록)",
            bg="#FFFFFF",
            fg="#334155",
            font=("Malgun Gothic", 8, "bold"),
            anchor="w",
        )
        add_lbl.pack(fill="x")
        add_row = tk.Frame(self.tab_student, bg="#FFFFFF")
        add_row.pack(fill="x", pady=(3, 12))
        self.add_cat_var = tk.StringVar(value=MEMORY_CATEGORY_LABELS["preference"])
        self.cmb_add_cat = ttk.Combobox(
            add_row,
            textvariable=self.add_cat_var,
            values=[MEMORY_CATEGORY_LABELS[c] for c in MEMORY_CATEGORIES],
            state="readonly",
            width=12,
        )
        self.cmb_add_cat.pack(side="left", padx=(0, 6))
        self.add_text_var = tk.StringVar()
        self.ent_add_mem = tk.Entry(
            add_row,
            textvariable=self.add_text_var,
            font=("Malgun Gothic", 9),
            bg="#F8FAFC",
            fg="#0F172A",
            relief="solid",
            bd=1,
        )
        self.ent_add_mem.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.ent_add_mem.bind("<Return>", lambda _e: self._on_add_manual_memory())
        tk.Button(
            add_row,
            text="+ 기억 추가",
            command=self._on_add_manual_memory,
            bg="#FB7390",
            fg="#FFFFFF",
            activebackground="#E45C7A",
            activeforeground="#FFFFFF",
            font=("Malgun Gothic", 9, "bold"),
            relief="flat",
            padx=10,
            pady=3,
            cursor="hand2",
        ).pack(side="right")

        # Pending review memories (anti-typo / contradiction hold queue) — WITH vertical scrollbar
        pf_hdr = tk.Frame(self.tab_student, bg="#FFFBEB", padx=8, pady=5)
        pf_hdr.pack(fill="x")
        tk.Button(
            pf_hdr,
            text="✕ 버리기",
            command=self._on_drop_pending,
            bg="#FEE2E2",
            fg="#991B1B",
            activebackground="#FECACA",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=7,
            pady=2,
            cursor="hand2",
        ).pack(side="right", padx=(4, 0))
        tk.Button(
            pf_hdr,
            text="✅ 확정 승격",
            command=self._on_approve_pending,
            bg="#DCFCE7",
            fg="#166534",
            activebackground="#BBF7D0",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=8,
            pady=2,
            cursor="hand2",
        ).pack(side="right", padx=(6, 0))
        self.lbl_pending_title = tk.Label(
            pf_hdr,
            text="⏳ 확인 대기 기억 (STT 불명확·1회성 모순 보류)",
            bg="#FFFBEB",
            fg="#92400E",
            font=("Malgun Gothic", 9, "bold"),
            anchor="w",
            justify="left",
        )
        self.lbl_pending_title.pack(side="left", fill="x", expand=True)
        bind_responsive_wrap(self.lbl_pending_title, container=pf_hdr, pad=180, min_wrap=160)

        pf_wrap = tk.Frame(self.tab_student, bg="#FFFDF5", highlightbackground="#FDE68A", highlightthickness=1)
        pf_wrap.pack(fill="x", pady=(0, 4))
        self.lst_pending = tk.Listbox(
            pf_wrap,
            height=4,
            font=("Malgun Gothic", 9),
            bg="#FFFDF5",
            fg="#78350F",
            selectbackground="#FEF3C7",
            selectforeground="#78350F",
            relief="flat",
            bd=0,
            activestyle="none",
            exportselection=False,
        )
        pf_sb = ttk.Scrollbar(pf_wrap, orient="vertical", command=self.lst_pending.yview)
        self.lst_pending.configure(yscrollcommand=pf_sb.set)
        pf_sb.pack(side="right", fill="y")
        self.lst_pending.pack(side="left", fill="both", expand=True, padx=4, pady=3)
        self.lst_pending.bind("<<ListboxSelect>>", self._on_pending_select)
        self.lst_pending.bind("<Double-Button-1>", lambda _e: self._on_approve_pending())
        self.lst_pending.bind("<Delete>", lambda _e: self._on_drop_pending())

        self.lbl_pending_preview = tk.Label(
            self.tab_student,
            text="",
            bg="#FFFFFF",
            fg="#92400E",
            font=("Malgun Gothic", 8),
            anchor="w",
            justify="left",
        )
        self.lbl_pending_preview.pack(fill="x", pady=(0, 8))
        bind_responsive_wrap(self.lbl_pending_preview, container=self.tab_student, pad=28, min_wrap=220)

        # Open threads / promises — WITH vertical scrollbar & live line counter
        th_hdr = tk.Frame(self.tab_student, bg="#FFFFFF")
        th_hdr.pack(fill="x", pady=(2, 3))
        tk.Label(
            th_hdr,
            text="📌 진행 중 화제 · 약속 (한 줄에 1개, 최대 5개)",
            bg="#FFFFFF",
            fg="#334155",
            font=("Malgun Gothic", 9, "bold"),
        ).pack(side="left")
        self.lbl_threads_count = tk.Label(
            th_hdr,
            text="0 / 5개",
            bg="#F1F5F9",
            fg="#475569",
            font=("Malgun Gothic", 8, "bold"),
            padx=6,
            pady=1,
        )
        self.lbl_threads_count.pack(side="left", padx=(6, 0))
        tk.Button(
            th_hdr,
            text="💾 화제 저장",
            command=self._on_save_threads,
            bg="#E2E8F0",
            fg="#1E293B",
            activebackground="#CBD5E1",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=8,
            pady=2,
            cursor="hand2",
        ).pack(side="right")

        th_wrap = tk.Frame(self.tab_student, bg="#F8FAFC", highlightbackground="#CBD5E1", highlightthickness=1)
        th_wrap.pack(fill="x", pady=(0, 12))
        self.txt_threads = tk.Text(
            th_wrap,
            height=4,
            font=("Malgun Gothic", 9),
            bg="#F8FAFC",
            fg="#0F172A",
            relief="flat",
            bd=0,
            wrap="word",
            padx=6,
            pady=4,
        )
        th_sb = ttk.Scrollbar(th_wrap, orient="vertical", command=self.txt_threads.yview)
        self.txt_threads.configure(yscrollcommand=th_sb.set)
        th_sb.pack(side="right", fill="y")
        self.txt_threads.pack(side="left", fill="both", expand=True)
        self.txt_threads.bind("<KeyRelease>", self._update_threads_count)

        # Safety rollback & reset bar
        bot = tk.Frame(
            self.tab_student,
            bg="#F8FAFC",
            highlightbackground="#E2E8F0",
            highlightthickness=1,
            padx=10,
            pady=8,
        )
        bot.pack(fill="x", pady=(0, 4))
        self.btn_rollback = tk.Button(
            bot,
            text="↩️ 직전 스냅샷 되돌리기",
            command=self._on_rollback_snapshot,
            bg="#E0E7FF",
            fg="#3730A3",
            activebackground="#C7D2FE",
            font=("Malgun Gothic", 9, "bold"),
            relief="flat",
            padx=10,
            pady=4,
            cursor="hand2",
        )
        self.btn_rollback.pack(side="left")
        tk.Button(
            bot,
            text="🧹 이 학생 장기 기억 초기화",
            command=self._on_clear_student_memory,
            bg="#FEF2F2",
            fg="#DC2626",
            activebackground="#FEE2E2",
            font=("Malgun Gothic", 9, "bold"),
            relief="flat",
            padx=10,
            pady=4,
            cursor="hand2",
        ).pack(side="right")

    def _switch_track(self, track: str) -> None:
        self.active_track = track
        self._selected_confirmed_id = ""
        self._refresh_student_tab()

    def _on_search_changed(self, *_args: Any) -> None:
        if self._suspend_search_trace or self._destroyed:
            return
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        self._apply_confirmed_filter()

    def _clear_search_filter(self) -> None:
        self._suspend_search_trace = True
        try:
            self.search_var.set("")
            self.filter_cat_var.set(_FILTER_ALL_LABEL)
        finally:
            self._suspend_search_trace = False
        self._apply_confirmed_filter()

    def _apply_confirmed_filter(self, *, keep_selected_id: str | None = None) -> None:
        target_id = keep_selected_id if keep_selected_id is not None else self._selected_confirmed_id
        query = self.search_var.get().strip().lower() if hasattr(self, "search_var") else ""
        cat_label = self.filter_cat_var.get() if hasattr(self, "filter_cat_var") else _FILTER_ALL_LABEL
        cat_filter = "" if cat_label == _FILTER_ALL_LABEL else _CAT_LABEL_TO_CODE.get(cat_label, "")

        self.lst_confirmed.delete(0, tk.END)
        self._confirmed_ids = []
        total = len(self._all_confirmed_items)
        reselect_idx: int | None = None

        for m in self._all_confirmed_items:
            m_cat = str(m.get("category", "fact"))
            m_txt = str(m.get("text", ""))
            cat_kr = MEMORY_CATEGORY_LABELS.get(m_cat, "정보")
            if cat_filter and m_cat != cat_filter:
                continue
            if query and query not in m_txt.lower() and query not in cat_kr.lower():
                continue
            mid = str(m.get("id") or "")
            src_tag = "직접" if m.get("source") == "manual" else f"자동·{max(1, _safe_int(m.get('mentions'), 1))}회"
            row_idx = len(self._confirmed_ids)
            self._confirmed_ids.append(mid)
            self.lst_confirmed.insert(tk.END, f"[{cat_kr}] {m_txt}  ({src_tag})")
            if target_id and mid == target_id:
                reselect_idx = row_idx

        shown = len(self._confirmed_ids)
        is_filtered = bool(query or cat_filter)
        if is_filtered:
            self.lbl_confirmed_title.configure(text=f"✅ 확정 장기 기억 (검색 {shown} / 전체 {total}개)")
        else:
            self.lbl_confirmed_title.configure(text=f"✅ 확정 장기 기억 ({total}개)")

        if reselect_idx is not None:
            self.lst_confirmed.selection_clear(0, tk.END)
            self.lst_confirmed.selection_set(reselect_idx)
            self.lst_confirmed.see(reselect_idx)
            self._populate_detail_editor(self._confirmed_ids[reselect_idx])
        else:
            self._selected_confirmed_id = ""
            self._clear_detail_editor(
                empty_hint=(
                    "🔍 검색/필터 조건과 일치하는 기억이 없습니다."
                    if (is_filtered and shown == 0)
                    else "📝 목록에서 기억을 클릭하면 전체 내용 확인 및 바로 수정할 수 있습니다."
                )
            )

    def _find_confirmed_item(self, mem_id: str) -> dict[str, Any] | None:
        if not mem_id:
            return None
        for m in self._all_confirmed_items:
            if str(m.get("id") or "") == mem_id:
                return m
        return None

    def _clear_detail_editor(self, empty_hint: str = "") -> None:
        self.lbl_detail_meta.configure(
            text=empty_hint or "📝 목록에서 기억을 클릭하면 전체 내용 확인 및 바로 수정할 수 있습니다."
        )
        self.txt_detail.configure(state="normal", bg="#F8FAFC")
        self.txt_detail.delete("1.0", tk.END)
        self.txt_detail.configure(state="disabled")
        self.cmb_detail_cat.configure(state="disabled")
        self.btn_save_detail.configure(state="disabled")
        self.btn_revert_detail.configure(state="disabled")
        self.lbl_detail_chars.configure(text=f"0 / {_MAX_MEMORY_CHARS}자", fg="#64748B")

    def _populate_detail_editor(self, mem_id: str) -> None:
        item = self._find_confirmed_item(mem_id)
        if item is None:
            self._selected_confirmed_id = ""
            self._clear_detail_editor()
            return
        self._selected_confirmed_id = mem_id
        cat_code = str(item.get("category") or "fact")
        cat_kr = MEMORY_CATEGORY_LABELS.get(cat_code, MEMORY_CATEGORY_LABELS["fact"])
        src_tag = "직접 입력" if item.get("source") == "manual" else f"자동 추출 · {max(1, _safe_int(item.get('mentions'), 1))}회 언급"
        upd_str = format_timestamp_safe(item.get("updated_at") or item.get("created_at"), "%Y-%m-%d %H:%M", "")
        meta_parts = [f"📝 선택 기억 상세 ({src_tag})"]
        if upd_str:
            meta_parts.append(f"수정: {upd_str}")
        self.lbl_detail_meta.configure(text="  ·  ".join(meta_parts))

        self.detail_cat_var.set(cat_kr)
        self.cmb_detail_cat.configure(state="readonly")
        self.txt_detail.configure(state="normal", bg="#FFFFFF")
        self.txt_detail.delete("1.0", tk.END)
        self.txt_detail.insert("1.0", str(item.get("text") or ""))
        self.btn_save_detail.configure(state="normal")
        self.btn_revert_detail.configure(state="normal")
        self._update_detail_char_count()

    def _update_detail_char_count(self, _event: Any = None) -> None:
        raw = self.txt_detail.get("1.0", "end-1c").strip()
        n = len(raw)
        color = "#DC2626" if n > _MAX_MEMORY_CHARS else "#64748B"
        self.lbl_detail_chars.configure(text=f"{n} / {_MAX_MEMORY_CHARS}자", fg=color)

    def _on_confirmed_select(self, _event: Any = None) -> None:
        sel = self.lst_confirmed.curselection()
        if not sel or not self.base_cid:
            return
        idx = int(sel[0])
        if 0 <= idx < len(self._confirmed_ids):
            self._populate_detail_editor(self._confirmed_ids[idx])

    def _revert_detail_editor(self) -> None:
        if self._selected_confirmed_id:
            self._populate_detail_editor(self._selected_confirmed_id)
            self._set_status("↩️ 편집 중인 내용을 원래 기억으로 되돌렸습니다.", "#475569")

    def _on_save_detail_edit(self) -> None:
        if not self.base_cid or not self._selected_confirmed_id:
            self._set_status("먼저 수정할 기억을 목록에서 선택해 주세요.", "#B45309")
            return
        new_text = self.txt_detail.get("1.0", "end-1c").strip()
        if not new_text:
            self._set_status("기억 내용이 비어 있습니다. 삭제하려면 [🗑️ 삭제]를 눌러주세요.", "#B45309")
            return
        cat_code = _CAT_LABEL_TO_CODE.get(self.detail_cat_var.get(), "fact")
        keep_id = self._selected_confirmed_id
        try:
            updated = self.memory_mgr.update_memory_text(
                self.base_cid,
                self.active_track,
                keep_id,
                new_text,
                category=cat_code,
            )
        except (OSError, ValueError) as exc:
            messagebox.showerror("기억 수정 실패", redact_secrets(str(exc)), parent=self)
            return
        if updated:
            self._refresh_student_tab(keep_selected_id=keep_id)
            self._notify_changed()
            self._set_status("✏️ 선택한 기억 내용과 분류를 저장했습니다.")
        else:
            self._refresh_student_tab()
            self._set_status("⚠️ 선택한 기억을 찾지 못해 목록을 새로고침했습니다.", "#B45309")

    def _on_pending_select(self, _event: Any = None) -> None:
        sel = self.lst_pending.curselection()
        if not sel:
            self.lbl_pending_preview.configure(text="")
            return
        idx = int(sel[0])
        if 0 <= idx < len(self._pending_items):
            pm = self._pending_items[idx]
            reason = str(pm.get("reason") or "확인 필요")
            txt = str(pm.get("text") or "")
            self.lbl_pending_preview.configure(text=f"선택된 보류 항목 [{reason}]: {txt}")

    def _update_threads_count(self, _event: Any = None) -> None:
        raw = self.txt_threads.get("1.0", tk.END)
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        n = len(lines)
        if n > 5:
            self.lbl_threads_count.configure(text=f"{n} / 5개 (상위 5개만 저장)", bg="#FEF3C7", fg="#92400E")
        else:
            self.lbl_threads_count.configure(text=f"{n} / 5개", bg="#F1F5F9", fg="#475569")

    def _refresh_student_tab(self, *, keep_selected_id: str | None = None) -> None:
        for tr_code, btn in self._track_btns.items():
            base_lbl = MEMORY_TRACK_LABELS[tr_code]
            if self.base_cid:
                try:
                    cnt = self.memory_mgr.get_student_memory_counts(self.base_cid, tr_code).get("confirmed", 0)
                    pill_lbl = f"{base_lbl} ({cnt})"
                except Exception:
                    pill_lbl = base_lbl
            else:
                pill_lbl = base_lbl
            if tr_code == self.active_track:
                btn.configure(text=pill_lbl, bg="#FB7390", fg="#FFFFFF")
            else:
                btn.configure(text=pill_lbl, bg="#F1F5F9", fg="#64748B")

        if not self.base_cid:
            self.lbl_student_meta.configure(text="왼쪽 모모톡 목록에서 학생을 먼저 선택해 주세요.")
            self._all_confirmed_items = []
            self._confirmed_ids = []
            self._pending_items = []
            self._pending_ids = []
            self._selected_confirmed_id = ""
            self.lst_confirmed.delete(0, tk.END)
            self.lst_pending.delete(0, tk.END)
            self.txt_threads.delete("1.0", tk.END)
            self._clear_detail_editor("왼쪽 모모톡 목록에서 학생을 먼저 선택해 주세요.")
            self.lbl_pending_preview.configure(text="")
            self._update_threads_count()
            self.btn_rollback.configure(state="disabled")
            self.tab_student_scroll.refresh_scrollregion(defer=True)
            return

        b = self.memory_mgr.get_student_bucket(self.base_cid, self.active_track)
        if not isinstance(b, dict):
            b = {}
        sessions = [s for s in (b.get("sessions") or []) if isinstance(s, dict) and s.get("turns")]
        total_calls = max(_safe_int(b.get("total_calls"), 0), len(sessions))
        last_str = format_timestamp_safe(b.get("last_called_at"), "%Y-%m-%d %H:%M", "없음")
        tr_lbl = MEMORY_TRACK_LABELS.get(self.active_track, self.active_track)
        self.lbl_student_meta.configure(
            text=f"{tr_lbl}  ·  누적 통화 {total_calls}회  ·  최근 통화: {last_str}"
        )

        # Confirmed list (filtered)
        self._all_confirmed_items = [m for m in (b.get("confirmed_memories") or []) if isinstance(m, dict)]
        self._apply_confirmed_filter(keep_selected_id=keep_selected_id)

        # Pending list
        self.lst_pending.delete(0, tk.END)
        self._pending_ids = []
        self._pending_items = [pm for pm in (b.get("pending_memories") or []) if isinstance(pm, dict)]
        self.lbl_pending_title.configure(text=f"⏳ 확인 대기 기억 ({len(self._pending_items)}개 · 오타/모순 보류)")
        for pm in self._pending_items:
            reason = str(pm.get("reason") or "확인 필요")
            self._pending_ids.append(str(pm.get("id") or ""))
            self.lst_pending.insert(tk.END, f"[{reason}] {pm.get('text', '')}")
        self.lbl_pending_preview.configure(
            text="💡 확인 대기 기억을 더블클릭하면 바로 확정 기억으로 승격됩니다." if self._pending_items else ""
        )

        # Open threads
        self.txt_threads.delete("1.0", tk.END)
        threads = [str(t).strip() for t in (b.get("open_threads") or []) if isinstance(t, str) and str(t).strip()]
        if threads:
            self.txt_threads.insert("1.0", "\n".join(threads))
        self._update_threads_count()

        # Rollback button state
        snaps = [s for s in (b.get("snapshots") or []) if isinstance(s, dict)]
        if snaps:
            latest = snaps[-1]
            sts_str = format_timestamp_safe(latest.get("created_at"), "%m/%d %H:%M", "")
            self.btn_rollback.configure(
                state="normal",
                text=f"↩️ 직전 스냅샷 되돌리기 ({sts_str} · {len(snaps)}개 보관)",
            )
        else:
            self.btn_rollback.configure(state="disabled", text="↩️ 직전 스냅샷 되돌리기 (없음)")

        self.tab_student_scroll.refresh_scrollregion(defer=True)

    def _on_add_manual_memory(self) -> None:
        if not self.base_cid:
            return
        txt = self.add_text_var.get().strip()
        if not txt:
            self._set_status("추가할 기억 내용을 입력해 주세요.", "#B45309")
            return
        cat = _CAT_LABEL_TO_CODE.get(self.add_cat_var.get(), "fact")
        try:
            added = self.memory_mgr.add_manual_memory(self.base_cid, self.active_track, txt, category=cat)
        except (OSError, ValueError) as exc:
            messagebox.showerror("기억 추가 실패", redact_secrets(str(exc)), parent=self)
            return
        self.add_text_var.set("")
        new_id = str((added or {}).get("id") or "")
        self._refresh_student_tab(keep_selected_id=new_id)
        self._notify_changed()
        self._set_status("✅ 새 장기 기억을 추가했습니다.")

    def _on_edit_confirmed(self) -> None:
        sel = self.lst_confirmed.curselection()
        if not sel or not self.base_cid:
            self._set_status("먼저 수정할 기억을 목록에서 선택해 주세요.", "#B45309")
            return
        idx = int(sel[0])
        if idx < 0 or idx >= len(self._confirmed_ids):
            return
        mid = self._confirmed_ids[idx]
        self._populate_detail_editor(mid)
        self.tab_student_scroll.scroll_to_widget(self.detail_card)
        self.txt_detail.focus_set()
        self.txt_detail.mark_set(tk.INSERT, tk.END)
        self._set_status("✏️ 아래 상세 편집창에서 수정한 뒤 [💾 수정 저장] 또는 Ctrl+Enter를 눌러주세요.", "#0369A1")

    def _on_delete_confirmed(self) -> None:
        sel = self.lst_confirmed.curselection()
        if not sel or not self.base_cid:
            self._set_status("먼저 삭제할 기억을 목록에서 선택해 주세요.", "#B45309")
            return
        idx = int(sel[0])
        if idx < 0 or idx >= len(self._confirmed_ids):
            return
        mid = self._confirmed_ids[idx]
        try:
            deleted = self.memory_mgr.delete_memory_item(self.base_cid, self.active_track, mid)
        except (OSError, ValueError) as exc:
            messagebox.showerror("기억 삭제 실패", redact_secrets(str(exc)), parent=self)
            return
        if deleted:
            if self._selected_confirmed_id == mid:
                self._selected_confirmed_id = ""
            self._refresh_student_tab()
            self._notify_changed()
            self._set_status("🗑️ 선택한 기억을 삭제했습니다.")

    def _on_approve_pending(self) -> None:
        sel = self.lst_pending.curselection()
        if not sel or not self.base_cid:
            self._set_status("먼저 확정 승격할 보류 기억을 선택해 주세요.", "#B45309")
            return
        idx = int(sel[0])
        if idx < 0 or idx >= len(self._pending_ids):
            return
        pid = self._pending_ids[idx]
        try:
            approved = self.memory_mgr.approve_pending_memory(self.base_cid, self.active_track, pid)
        except (OSError, ValueError) as exc:
            messagebox.showerror("확정 승격 실패", redact_secrets(str(exc)), parent=self)
            return
        if approved:
            self._refresh_student_tab()
            self._notify_changed()
            self._set_status("✅ 보류 기억을 확정 장기 기억으로 승격했습니다.")

    def _on_drop_pending(self) -> None:
        sel = self.lst_pending.curselection()
        if not sel or not self.base_cid:
            self._set_status("먼저 버릴 보류 기억을 선택해 주세요.", "#B45309")
            return
        idx = int(sel[0])
        if idx < 0 or idx >= len(self._pending_ids):
            return
        pid = self._pending_ids[idx]
        try:
            dropped = self.memory_mgr.delete_memory_item(self.base_cid, self.active_track, pid)
        except (OSError, ValueError) as exc:
            messagebox.showerror("보류 기억 삭제 실패", redact_secrets(str(exc)), parent=self)
            return
        if dropped:
            self._refresh_student_tab()
            self._notify_changed()
            self._set_status("✕ 보류 기억을 버렸습니다.")

    def _on_save_threads(self) -> None:
        if not self.base_cid:
            return
        raw = self.txt_threads.get("1.0", tk.END)
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        try:
            self.memory_mgr.update_open_threads(self.base_cid, self.active_track, lines)
        except (OSError, ValueError) as exc:
            messagebox.showerror("화제 저장 실패", redact_secrets(str(exc)), parent=self)
            return
        self._refresh_student_tab()
        self._notify_changed()
        self._set_status("💾 진행 중 화제·약속을 저장했습니다.")

    def _on_rollback_snapshot(self) -> None:
        if not self.base_cid:
            return
        if not messagebox.askyesno(
            "스냅샷 되돌리기",
            "가장 최근 자동 요약 이전 시점의 기억으로 되돌릴까요?",
            parent=self,
        ):
            return
        try:
            ok, msg = self.memory_mgr.rollback_to_snapshot(self.base_cid, self.active_track)
        except (OSError, ValueError) as exc:
            messagebox.showerror("스냅샷 되돌리기 실패", redact_secrets(str(exc)), parent=self)
            return
        self._selected_confirmed_id = ""
        self._refresh_student_tab()
        self._notify_changed()
        self._set_status(f"↩️ {msg}" if ok else msg, "#0F766E" if ok else "#DC2626")

    def _on_clear_student_memory(self) -> None:
        if not self.base_cid:
            return
        tr_lbl = MEMORY_TRACK_LABELS.get(self.active_track, self.active_track)
        ans = messagebox.askyesnocancel(
            "장기 기억 초기화",
            f"'{self.student_name_kr}' ({tr_lbl})의 기억을 어떻게 초기화할까요?\n\n"
            "• [예]: 장기 기억만 초기화 (지난 대화 기록은 유지 · 스냅샷 자동 백업)\n"
            "• [아니오]: 장기 기억 + 지난 대화 기록까지 모두 삭제\n"
            "• [취소]: 취소",
            parent=self,
        )
        if ans is None:
            return
        include_sessions = not bool(ans)
        try:
            self.memory_mgr.clear_student_memory(self.base_cid, self.active_track, include_sessions=include_sessions)
        except (OSError, ValueError) as exc:
            messagebox.showerror("기억 초기화 실패", redact_secrets(str(exc)), parent=self)
            return
        self._selected_confirmed_id = ""
        self._refresh_student_tab()
        self._notify_changed()
        self._set_status(
            "🧹 장기 기억과 지난 대화 기록을 모두 초기화했습니다."
            if include_sessions
            else "🧹 장기 기억을 초기화했습니다 (스냅샷으로 되돌리기 가능)."
        )

    # ------------------------------------------------------------------
    # Tab 1: Shared Sensei Profile
    # ------------------------------------------------------------------

    def _build_sensei_tab(self) -> None:
        lbl_intro = tk.Label(
            self.tab_sensei,
            text="여기에 적어둔 정보는 모든 학생이 공통으로 기억하는 '선생님 기본 프로필'로 활용됩니다.",
            bg="#FFFFFF",
            fg="#475569",
            font=("Malgun Gothic", 9),
            anchor="w",
            justify="left",
        )
        lbl_intro.pack(fill="x", pady=(0, 12))
        bind_responsive_wrap(lbl_intro, container=self.tab_sensei, pad=24, min_wrap=220)

        lbl_nick = tk.Label(
            self.tab_sensei,
            text="선생님 호칭 / 별명 (비워두면 각 학생의 기본 호칭 사용)",
            bg="#FFFFFF",
            fg="#1E293B",
            font=("Malgun Gothic", 9, "bold"),
            anchor="w",
            justify="left",
        )
        lbl_nick.pack(fill="x")
        bind_responsive_wrap(lbl_nick, container=self.tab_sensei, pad=24, min_wrap=220)
        self.sp_nick_var = tk.StringVar()
        tk.Entry(
            self.tab_sensei,
            textvariable=self.sp_nick_var,
            font=("Malgun Gothic", 10),
            bg="#F8FAFC",
            fg="#0F172A",
            relief="solid",
            bd=1,
        ).pack(fill="x", pady=(4, 12))

        prefs_hdr = tk.Frame(self.tab_sensei, bg="#FFFFFF")
        prefs_hdr.pack(fill="x")
        self.lbl_sp_prefs_count = tk.Label(
            prefs_hdr,
            text="0개",
            bg="#F1F5F9",
            fg="#475569",
            font=("Malgun Gothic", 8, "bold"),
            padx=6,
            pady=1,
        )
        self.lbl_sp_prefs_count.pack(side="right", padx=(6, 0))
        lbl_prefs = tk.Label(
            prefs_hdr,
            text="관심사 · 취향 키워드 (쉼표로 구분 · 예: 아메리카노, 야간 작업, 일본어 공부, 고양이)",
            bg="#FFFFFF",
            fg="#1E293B",
            font=("Malgun Gothic", 9, "bold"),
            anchor="w",
            justify="left",
        )
        lbl_prefs.pack(side="left", fill="x", expand=True)
        bind_responsive_wrap(lbl_prefs, container=prefs_hdr, pad=90, min_wrap=200)

        self.sp_prefs_var = tk.StringVar(master=self)
        self._sp_prefs_trace_token = self.sp_prefs_var.trace_add("write", lambda *_a: self._update_sensei_counts())
        tk.Entry(
            self.tab_sensei,
            textvariable=self.sp_prefs_var,
            font=("Malgun Gothic", 10),
            bg="#F8FAFC",
            fg="#0F172A",
            relief="solid",
            bd=1,
        ).pack(fill="x", pady=(4, 12))

        bio_hdr = tk.Frame(self.tab_sensei, bg="#FFFFFF")
        bio_hdr.pack(fill="x")
        self.lbl_sp_bio_count = tk.Label(
            bio_hdr,
            text="0자",
            bg="#F1F5F9",
            fg="#475569",
            font=("Malgun Gothic", 8, "bold"),
            padx=6,
            pady=1,
        )
        self.lbl_sp_bio_count.pack(side="right", padx=(6, 0))
        lbl_bio = tk.Label(
            bio_hdr,
            text="선생님 소개 · 생활 리듬 메모 (모든 학생이 알고 있으면 좋은 배경 정보)",
            bg="#FFFFFF",
            fg="#1E293B",
            font=("Malgun Gothic", 9, "bold"),
            anchor="w",
            justify="left",
        )
        lbl_bio.pack(side="left", fill="x", expand=True)
        bind_responsive_wrap(lbl_bio, container=bio_hdr, pad=90, min_wrap=200)

        bio_wrap = tk.Frame(self.tab_sensei, bg="#F8FAFC", highlightbackground="#CBD5E1", highlightthickness=1)
        bio_wrap.pack(fill="x", pady=(4, 12))
        self.txt_sp_bio = tk.Text(
            bio_wrap,
            height=9,
            font=("Malgun Gothic", 10),
            bg="#F8FAFC",
            fg="#0F172A",
            relief="flat",
            bd=0,
            wrap="word",
            padx=6,
            pady=6,
        )
        bio_sb = ttk.Scrollbar(bio_wrap, orient="vertical", command=self.txt_sp_bio.yview)
        self.txt_sp_bio.configure(yscrollcommand=bio_sb.set)
        bio_sb.pack(side="right", fill="y")
        self.txt_sp_bio.pack(side="left", fill="both", expand=True)
        self.txt_sp_bio.bind("<KeyRelease>", lambda _e: self._update_sensei_counts())

        save_row = tk.Frame(self.tab_sensei, bg="#FFFFFF")
        save_row.pack(fill="x", pady=(2, 4))
        tk.Label(
            save_row,
            text="💡 Ctrl+S를 눌러도 바로 저장됩니다.",
            bg="#FFFFFF",
            fg="#64748B",
            font=("Malgun Gothic", 8),
        ).pack(side="left")
        tk.Button(
            save_row,
            text="💾 공통 선생님 프로필 저장",
            command=self._on_save_sensei_profile,
            bg="#FB7390",
            fg="#FFFFFF",
            activebackground="#E45C7A",
            activeforeground="#FFFFFF",
            font=("Malgun Gothic", 10, "bold"),
            relief="flat",
            padx=16,
            pady=6,
            cursor="hand2",
        ).pack(side="right")

    def _update_sensei_counts(self) -> None:
        if self._destroyed:
            return
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        prefs = [p.strip() for p in self.sp_prefs_var.get().split(",") if p.strip()]
        if hasattr(self, "lbl_sp_prefs_count"):
            self.lbl_sp_prefs_count.configure(text=f"{len(prefs)}개")
        if hasattr(self, "lbl_sp_bio_count"):
            bio_len = len(self.txt_sp_bio.get("1.0", "end-1c").strip())
            self.lbl_sp_bio_count.configure(text=f"{bio_len}자")

    def _load_sensei_profile_into_ui(self) -> None:
        sp = self.memory_mgr.load_sensei_profile()
        if not isinstance(sp, dict):
            sp = {}
        self.sp_nick_var.set(str(sp.get("nickname") or ""))
        prefs = [str(p).strip() for p in (sp.get("preferences") or []) if isinstance(p, str) and p.strip()]
        self.sp_prefs_var.set(", ".join(prefs))
        self.txt_sp_bio.delete("1.0", tk.END)
        if sp.get("bio_notes"):
            self.txt_sp_bio.insert("1.0", str(sp["bio_notes"]))
        self._update_sensei_counts()
        self.tab_sensei_scroll.refresh_scrollregion(defer=True)

    def _on_save_sensei_profile(self) -> None:
        prefs = [p.strip() for p in self.sp_prefs_var.get().split(",") if p.strip()]
        bio = self.txt_sp_bio.get("1.0", tk.END).strip()
        try:
            self.memory_mgr.save_sensei_profile(
                nickname=self.sp_nick_var.get(),
                bio_notes=bio,
                preferences=prefs,
            )
        except (OSError, ValueError) as exc:
            messagebox.showerror("프로필 저장 실패", redact_secrets(str(exc)), parent=self)
            return
        self._notify_changed()
        self._set_status("💾 공통 선생님 프로필을 저장했습니다 (모든 학생에게 반영).")

    # ------------------------------------------------------------------
    # Tab 2: JSON Backup & Restore
    # ------------------------------------------------------------------

    def _build_backup_tab(self) -> None:
        lbl_desc = tk.Label(
            self.tab_backup,
            text="모든 대화 기록과 장기 기억은 PC의 개인 데이터 폴더에만 안전하게 격리 저장됩니다.",
            bg="#FFFFFF",
            fg="#475569",
            font=("Malgun Gothic", 9),
            anchor="w",
            justify="left",
        )
        lbl_desc.pack(fill="x", pady=(0, 8))
        bind_responsive_wrap(lbl_desc, container=self.tab_backup, pad=24, min_wrap=220)

        path_box = tk.Frame(
            self.tab_backup,
            bg="#F8FAFC",
            padx=10,
            pady=8,
            highlightbackground="#E2E8F0",
            highlightthickness=1,
        )
        path_box.pack(fill="x", pady=(0, 10))
        # Pack right-side buttons BEFORE expanding path label so buttons never squeeze at min_w
        tk.Button(
            path_box,
            text="📂 폴더 열기",
            command=self._open_memory_folder,
            bg="#E2E8F0",
            fg="#1E293B",
            activebackground="#CBD5E1",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=8,
            pady=3,
            cursor="hand2",
        ).pack(side="right")
        tk.Button(
            path_box,
            text="📋 경로 복사",
            command=self._copy_memory_folder_path,
            bg="#E0F2FE",
            fg="#0369A1",
            activebackground="#BAE6FD",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=8,
            pady=3,
            cursor="hand2",
        ).pack(side="right", padx=(8, 6))
        lbl_path = tk.Label(
            path_box,
            text=str(self.memory_mgr.memory_dir),
            bg="#F8FAFC",
            fg="#334155",
            font=("Consolas", 9),
            anchor="w",
            justify="left",
        )
        lbl_path.pack(side="left", fill="x", expand=True)
        bind_responsive_wrap(lbl_path, container=path_box, pad=200, min_wrap=160)

        self.lbl_backup_summary = tk.Label(
            self.tab_backup,
            text="",
            bg="#F8FAFC",
            fg="#334155",
            font=("Malgun Gothic", 8, "bold"),
            anchor="w",
            justify="left",
            padx=10,
            pady=6,
        )
        self.lbl_backup_summary.pack(fill="x", pady=(0, 12))
        bind_responsive_wrap(self.lbl_backup_summary, container=self.tab_backup, pad=36, min_wrap=220)

        exp_frame = tk.LabelFrame(
            self.tab_backup,
            text=" 📤 JSON 백업 내보내기 ",
            bg="#FFFFFF",
            fg="#1E293B",
            font=("Malgun Gothic", 9, "bold"),
            padx=12,
            pady=10,
        )
        exp_frame.pack(fill="x", pady=(0, 12))
        lbl_exp_hint = tk.Label(
            exp_frame,
            text="PC를 옮기거나 중요한 기억을 보관하고 싶을 때 JSON 파일로 안전하게 백업할 수 있습니다.",
            bg="#FFFFFF",
            fg="#64748B",
            font=("Malgun Gothic", 8),
            anchor="w",
            justify="left",
        )
        lbl_exp_hint.pack(fill="x", pady=(0, 8))
        bind_responsive_wrap(lbl_exp_hint, container=exp_frame, pad=28, min_wrap=200)

        tk.Button(
            exp_frame,
            text="📤 전체 학생 기억 · 대화 기록 백업 (JSON)",
            command=lambda: self._on_export_backup(single_student=False),
            bg="#0EA5E9",
            fg="#FFFFFF",
            activebackground="#0284C7",
            activeforeground="#FFFFFF",
            font=("Malgun Gothic", 9, "bold"),
            relief="flat",
            padx=14,
            pady=6,
            cursor="hand2",
        ).pack(anchor="w", pady=(0, 6))
        self.btn_export_single = tk.Button(
            exp_frame,
            text=f"📤 '{self.student_name_kr}' 기억 · 기록만 백업 (JSON)",
            command=lambda: self._on_export_backup(single_student=True),
            state="normal" if self.base_cid else "disabled",
            bg="#F1F5F9",
            fg="#1E293B",
            activebackground="#E2E8F0",
            font=("Malgun Gothic", 9, "bold"),
            relief="flat",
            padx=14,
            pady=5,
            cursor="hand2",
        )
        self.btn_export_single.pack(anchor="w")

        imp_frame = tk.LabelFrame(
            self.tab_backup,
            text=" 📥 JSON 백업 복원하기 ",
            bg="#FFFFFF",
            fg="#1E293B",
            font=("Malgun Gothic", 9, "bold"),
            padx=12,
            pady=10,
        )
        imp_frame.pack(fill="x", pady=(0, 4))
        lbl_imp_hint = tk.Label(
            imp_frame,
            text="• 병합(권장): 현재 기록을 유지하면서 백업의 누락된 기억·대화를 합칩니다.\n"
            "• 덮어쓰기: 백업에 포함된 학생의 기록을 백업 시점 상태로 교체합니다.",
            bg="#FFFFFF",
            fg="#64748B",
            font=("Malgun Gothic", 8),
            anchor="w",
            justify="left",
        )
        lbl_imp_hint.pack(fill="x", pady=(0, 8))
        bind_responsive_wrap(lbl_imp_hint, container=imp_frame, pad=28, min_wrap=200)

        tk.Button(
            imp_frame,
            text="📥 백업 불러오기 — 기존 기록과 병합 (권장)",
            command=lambda: self._on_import_backup(merge=True),
            bg="#16A34A",
            fg="#FFFFFF",
            activebackground="#15803D",
            activeforeground="#FFFFFF",
            font=("Malgun Gothic", 9, "bold"),
            relief="flat",
            padx=14,
            pady=6,
            cursor="hand2",
        ).pack(anchor="w", pady=(0, 6))
        tk.Button(
            imp_frame,
            text="⚠️ 백업 불러오기 — 덮어쓰기",
            command=lambda: self._on_import_backup(merge=False),
            bg="#FEF2F2",
            fg="#DC2626",
            activebackground="#FEE2E2",
            font=("Malgun Gothic", 9, "bold"),
            relief="flat",
            padx=14,
            pady=5,
            cursor="hand2",
        ).pack(anchor="w")

    def _refresh_backup_summary(self) -> None:
        if not hasattr(self, "lbl_backup_summary"):
            return
        try:
            sdir = self.memory_mgr.students_dir
            bucket_files = [p for p in sdir.glob("*.json") if ".corrupt." not in p.name] if sdir.is_dir() else []
            recent_cnt = len(self.memory_mgr.get_recent_entry_ids())
            extra = ""
            if self.base_cid:
                c = self.memory_mgr.get_student_memory_counts(self.base_cid, self.active_track)
                extra = f"  |  현재 학생({self.student_name_kr}): 확정 {c['confirmed']} · 보류 {c['pending']} · 통화 {c['sessions']}건"
            self.lbl_backup_summary.configure(
                text=f"📊 저장 현황: 학생 기억 파일 {len(bucket_files)}개 · 최근 통화 학생 {recent_cnt}명{extra}"
            )
        except Exception:
            self.lbl_backup_summary.configure(text="📊 저장 현황: 로컬 개인 데이터 폴더에 격리 저장 중")
        self.tab_backup_scroll.refresh_scrollregion(defer=True)

    def _copy_memory_folder_path(self) -> None:
        try:
            self.clipboard_clear()
            self.clipboard_append(str(self.memory_mgr.memory_dir))
            self._set_status("📋 기억 데이터 저장 경로를 클립보드에 복사했습니다.")
        except Exception:
            pass

    def _open_memory_folder(self) -> None:
        folder = self.memory_mgr.memory_dir
        try:
            resolved = _validate_open_folder_target(folder, self.memory_mgr.memory_dir)
            if os.name == "nt":
                os.startfile(str(resolved), "explore")  # type: ignore[attr-defined]
            else:
                if not str(resolved).startswith("/"):
                    raise ValueError("Invalid directory path")
                subprocess.Popen(["xdg-open", str(resolved)], shell=False)
        except Exception as exc:
            messagebox.showerror("폴더 열기 실패", redact_secrets(str(exc)), parent=self)

    def _on_export_backup(self, single_student: bool) -> None:
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
        default_name = _safe_backup_filename(self.base_cid, stamp, single_student)
        dest = filedialog.asksaveasfilename(
            parent=self,
            title="모모톡 기억 백업 저장",
            defaultextension=".json",
            initialfile=default_name,
            filetypes=[("JSON 파일", "*.json")],
        )
        if not dest:
            return
        try:
            if "\x00" in str(dest):
                raise ValueError("유효하지 않은 저장 경로입니다.")
            dest_path = Path(dest)
            if dest_path.is_symlink():
                raise ValueError("심볼릭 링크 대상에는 백업을 저장할 수 없습니다.")
            if dest_path.suffix.lower() != ".json":
                raise ValueError("백업 파일은 .json 확장자만 저장할 수 있습니다.")
            stats = self.memory_mgr.export_backup(
                dest_path,
                base_cid=self.base_cid if single_student else None,
                track=self.active_track if single_student else None,
            )
            self._set_status(
                f"📤 백업 완료: 학생버킷 {stats['buckets']}개 · 기억 {stats['memories']}개 · 통화 {stats['sessions']}건"
            )
        except Exception as exc:
            messagebox.showerror("백업 실패", redact_secrets(str(exc)), parent=self)

    def _on_import_backup(self, merge: bool) -> None:
        if not merge:
            if not messagebox.askyesno(
                "덮어쓰기 확인",
                "백업 파일에 포함된 학생의 기존 기억과 대화 기록을 덮어씁니다. 계속할까요?",
                parent=self,
            ):
                return
        src = filedialog.askopenfilename(
            parent=self,
            title="모모톡 기억 백업 파일 선택",
            filetypes=[("JSON 파일", "*.json")],
        )
        if not src:
            return
        try:
            if "\x00" in str(src):
                raise ValueError("유효하지 않은 파일 경로입니다.")
            src_path = Path(src)
            if src_path.is_symlink() or not src_path.is_file():
                raise ValueError("유효한 일반 JSON 백업 파일만 불러올 수 있습니다.")
            stats = self.memory_mgr.import_backup(src_path, merge=merge)
            self._refresh_student_tab()
            self._load_sensei_profile_into_ui()
            self._refresh_backup_summary()
            self._notify_changed()
            self._set_status(
                f"📥 복원 완료: 버킷 {stats['buckets']}개 · 기억 {stats['memories']}개 · 통화 {stats['sessions']}건"
            )
        except Exception as exc:
            messagebox.showerror("복원 실패", redact_secrets(str(exc)), parent=self)
