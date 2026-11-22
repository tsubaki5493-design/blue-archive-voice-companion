"""
MomoTalk Memory Note & Shared Sensei Profile Dialog
---------------------------------------------------
Clean MomoTalk-themed modal (`tk.Toplevel`) giving users transparent control over:
  - Tab 0: Per-student structured memories (Confirmed, Pending STT-review hold, Open Threads),
           1-click snapshot rollback, manual add/edit/delete, and safe track reset
  - Tab 1: Shared Sensei Profile (known across all 196 students)
  - Tab 2: JSON Backup / Restore & isolated storage folder access
"""

from __future__ import annotations

import datetime
import os
import subprocess
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any, Callable

from src.controller.memory_manager import (
    MEMORY_CATEGORIES,
    MEMORY_CATEGORY_LABELS,
    MEMORY_TRACK_LABELS,
    MemoryManager,
)

_CAT_LABEL_TO_CODE: dict[str, str] = {v: k for k, v in MEMORY_CATEGORY_LABELS.items()}


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
        self.base_cid = (base_cid or "").strip()
        self.student_name_kr = (student_name_kr or base_cid or "학생").strip()
        self.active_track = track if track in ("official", "fanon", "custom") else "official"
        self.on_changed = on_changed
        self._confirmed_ids: list[str] = []
        self._pending_ids: list[str] = []

        self.title(f"MomoTalk 기억 노트 — {self.student_name_kr}")
        self.configure(bg="#F6F8FB")
        self.geometry("760x660")
        self.minsize(680, 560)
        self.transient(parent)
        self.grab_set()

        self._build_ui(initial_tab=max(0, min(2, int(initial_tab))))
        self._refresh_student_tab()
        self._load_sensei_profile_into_ui()

        self.bind("<Escape>", lambda _e: self.destroy())
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def _notify_changed(self) -> None:
        if callable(self.on_changed):
            try:
                self.on_changed()
            except Exception:
                pass

    def _build_ui(self, initial_tab: int = 0) -> None:
        # Top MomoTalk coral header
        hdr = tk.Frame(self, bg="#FB7390", padx=16, pady=10)
        hdr.pack(fill="x")
        tk.Label(
            hdr,
            text=f"🧠 기억 노트 & 선생님 프로필",
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

        body = tk.Frame(self, bg="#F6F8FB", padx=12, pady=10)
        body.pack(fill="both", expand=True)

        self.notebook = ttk.Notebook(body)
        self.notebook.pack(fill="both", expand=True)

        self.tab_student = tk.Frame(self.notebook, bg="#FFFFFF", padx=14, pady=12)
        self.tab_sensei = tk.Frame(self.notebook, bg="#FFFFFF", padx=16, pady=14)
        self.tab_backup = tk.Frame(self.notebook, bg="#FFFFFF", padx=16, pady=14)

        self.notebook.add(self.tab_student, text=f"🧠 {self.student_name_kr} 기억")
        self.notebook.add(self.tab_sensei, text="👤 공통 선생님 프로필")
        self.notebook.add(self.tab_backup, text="💾 백업 · 복원")

        self._build_student_tab()
        self._build_sensei_tab()
        self._build_backup_tab()

        if not self.base_cid and initial_tab == 0:
            initial_tab = 1
        self.notebook.select(initial_tab)

        footer = tk.Frame(self, bg="#F6F8FB", padx=14, pady=8)
        footer.pack(fill="x")
        self.lbl_dialog_status = tk.Label(
            footer,
            text="",
            bg="#F6F8FB",
            fg="#0F766E",
            font=("Malgun Gothic", 9, "bold"),
            anchor="w",
        )
        self.lbl_dialog_status.pack(side="left", fill="x", expand=True)
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

    def _set_status(self, msg: str, color: str = "#0F766E") -> None:
        self.lbl_dialog_status.configure(text=msg, fg=color)

    # ------------------------------------------------------------------
    # Tab 0: Student Memory Note
    # ------------------------------------------------------------------

    def _build_student_tab(self) -> None:
        top = tk.Frame(self.tab_student, bg="#FFFFFF")
        top.pack(fill="x", pady=(0, 8))

        self.lbl_student_meta = tk.Label(
            top,
            text="",
            bg="#FFFFFF",
            fg="#334155",
            font=("Malgun Gothic", 9, "bold"),
            anchor="w",
        )
        self.lbl_student_meta.pack(side="left", fill="x", expand=True)

        track_pill = tk.Frame(top, bg="#F1F5F9", padx=3, pady=3)
        track_pill.pack(side="right")
        self._track_btns: dict[str, tk.Button] = {}
        for tr_code in ("official", "fanon"):
            btn = tk.Button(
                track_pill,
                text=MEMORY_TRACK_LABELS[tr_code],
                command=lambda c=tr_code: self._switch_track(c),
                font=("Malgun Gothic", 8, "bold"),
                relief="flat",
                bd=0,
                padx=8,
                pady=2,
                cursor="hand2",
            )
            btn.pack(side="left", padx=2)
            self._track_btns[tr_code] = btn

        # Confirmed memories section
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
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=8,
            pady=2,
            cursor="hand2",
        ).pack(side="right")

        cf_wrap = tk.Frame(self.tab_student, bg="#FFFFFF", highlightbackground="#CBD5E1", highlightthickness=1)
        cf_wrap.pack(fill="both", expand=True)
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
        )
        cf_sb = ttk.Scrollbar(cf_wrap, orient="vertical", command=self.lst_confirmed.yview)
        self.lst_confirmed.configure(yscrollcommand=cf_sb.set)
        cf_sb.pack(side="right", fill="y")
        self.lst_confirmed.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        self.lst_confirmed.bind("<Double-Button-1>", lambda _e: self._on_edit_confirmed())

        # Quick manual add bar
        add_row = tk.Frame(self.tab_student, bg="#FFFFFF")
        add_row.pack(fill="x", pady=(6, 10))
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

        # Pending review memories (anti-typo / contradiction hold queue)
        pf_hdr = tk.Frame(self.tab_student, bg="#FFFBEB", padx=8, pady=4)
        pf_hdr.pack(fill="x")
        self.lbl_pending_title = tk.Label(
            pf_hdr,
            text="⏳ 확인 대기 기억 (STT 불명확·1회성 모순 보류)",
            bg="#FFFBEB",
            fg="#92400E",
            font=("Malgun Gothic", 9, "bold"),
        )
        self.lbl_pending_title.pack(side="left")
        tk.Button(
            pf_hdr,
            text="✕ 버리기",
            command=self._on_drop_pending,
            bg="#FEE2E2",
            fg="#991B1B",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=7,
            pady=1,
            cursor="hand2",
        ).pack(side="right", padx=(4, 0))
        tk.Button(
            pf_hdr,
            text="✅ 확정 승격",
            command=self._on_approve_pending,
            bg="#DCFCE7",
            fg="#166534",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=8,
            pady=1,
            cursor="hand2",
        ).pack(side="right")

        pf_wrap = tk.Frame(self.tab_student, bg="#FFFDF5", highlightbackground="#FDE68A", highlightthickness=1)
        pf_wrap.pack(fill="x", pady=(0, 8))
        self.lst_pending = tk.Listbox(
            pf_wrap,
            height=3,
            font=("Malgun Gothic", 9),
            bg="#FFFDF5",
            fg="#78350F",
            selectbackground="#FEF3C7",
            selectforeground="#78350F",
            relief="flat",
            bd=0,
            activestyle="none",
        )
        self.lst_pending.pack(fill="x", padx=4, pady=3)

        # Open threads / promises
        th_hdr = tk.Frame(self.tab_student, bg="#FFFFFF")
        th_hdr.pack(fill="x", pady=(2, 3))
        tk.Label(
            th_hdr,
            text="📌 진행 중 화제 · 약속 (한 줄에 1개, 최대 5개)",
            bg="#FFFFFF",
            fg="#334155",
            font=("Malgun Gothic", 9, "bold"),
        ).pack(side="left")
        tk.Button(
            th_hdr,
            text="💾 화제 저장",
            command=self._on_save_threads,
            bg="#E2E8F0",
            fg="#1E293B",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=8,
            pady=2,
            cursor="hand2",
        ).pack(side="right")
        self.txt_threads = tk.Text(
            self.tab_student,
            height=3,
            font=("Malgun Gothic", 9),
            bg="#F8FAFC",
            fg="#0F172A",
            relief="solid",
            bd=1,
            wrap="word",
        )
        self.txt_threads.pack(fill="x", pady=(0, 8))

        # Safety rollback & reset bar
        bot = tk.Frame(self.tab_student, bg="#FFFFFF")
        bot.pack(fill="x")
        self.btn_rollback = tk.Button(
            bot,
            text="↩️ 직전 스냅샷 되돌리기",
            command=self._on_rollback_snapshot,
            bg="#E0E7FF",
            fg="#3730A3",
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
            font=("Malgun Gothic", 9, "bold"),
            relief="flat",
            padx=10,
            pady=4,
            cursor="hand2",
        ).pack(side="right")

    def _switch_track(self, track: str) -> None:
        self.active_track = track
        self._refresh_student_tab()

    def _refresh_student_tab(self) -> None:
        for tr_code, btn in self._track_btns.items():
            if tr_code == self.active_track:
                btn.configure(bg="#FB7390", fg="#FFFFFF")
            else:
                btn.configure(bg="#F1F5F9", fg="#64748B")

        if not self.base_cid:
            self.lbl_student_meta.configure(text="왼쪽 모모톡 목록에서 학생을 먼저 선택해 주세요.")
            self.lst_confirmed.delete(0, tk.END)
            self.lst_pending.delete(0, tk.END)
            self.btn_rollback.configure(state="disabled")
            return

        b = self.memory_mgr.get_student_bucket(self.base_cid, self.active_track)
        sessions = [s for s in (b.get("sessions") or []) if s.get("turns")]
        total_calls = max(int(b.get("total_calls", 0) or 0), len(sessions))
        last_ts = float(b.get("last_called_at", 0.0) or 0.0)
        last_str = datetime.datetime.fromtimestamp(last_ts).strftime("%Y-%m-%d %H:%M") if last_ts > 0 else "없음"
        tr_lbl = MEMORY_TRACK_LABELS.get(self.active_track, self.active_track)
        self.lbl_student_meta.configure(
            text=f"{tr_lbl}  ·  누적 통화 {total_calls}회  ·  최근 통화: {last_str}"
        )

        # Confirmed list
        self.lst_confirmed.delete(0, tk.END)
        self._confirmed_ids = []
        confirmed = b.get("confirmed_memories") or []
        self.lbl_confirmed_title.configure(text=f"✅ 확정 장기 기억 ({len(confirmed)}개)")
        for m in confirmed:
            cat_kr = MEMORY_CATEGORY_LABELS.get(str(m.get("category", "fact")), "정보")
            src_tag = "직접" if m.get("source") == "manual" else f"자동·{int(m.get('mentions', 1) or 1)}회"
            self._confirmed_ids.append(str(m.get("id") or ""))
            self.lst_confirmed.insert(tk.END, f"[{cat_kr}] {m.get('text', '')}  ({src_tag})")

        # Pending list
        self.lst_pending.delete(0, tk.END)
        self._pending_ids = []
        pending = b.get("pending_memories") or []
        self.lbl_pending_title.configure(text=f"⏳ 확인 대기 기억 ({len(pending)}개 · 오타/모순 보류)")
        for pm in pending:
            reason = str(pm.get("reason") or "확인 필요")
            self._pending_ids.append(str(pm.get("id") or ""))
            self.lst_pending.insert(tk.END, f"[{reason}] {pm.get('text', '')}")

        # Open threads
        self.txt_threads.delete("1.0", tk.END)
        threads = [str(t).strip() for t in (b.get("open_threads") or []) if str(t).strip()]
        if threads:
            self.txt_threads.insert("1.0", "\n".join(threads))

        # Rollback button state
        snaps = b.get("snapshots") or []
        if snaps:
            latest = snaps[-1]
            sts = float(latest.get("created_at") or 0.0)
            sts_str = datetime.datetime.fromtimestamp(sts).strftime("%m/%d %H:%M") if sts else ""
            self.btn_rollback.configure(
                state="normal",
                text=f"↩️ 직전 스냅샷 되돌리기 ({sts_str} · {len(snaps)}개 보관)",
            )
        else:
            self.btn_rollback.configure(state="disabled", text="↩️ 직전 스냅샷 되돌리기 (없음)")

    def _on_add_manual_memory(self) -> None:
        if not self.base_cid:
            return
        txt = self.add_text_var.get().strip()
        if not txt:
            return
        cat = _CAT_LABEL_TO_CODE.get(self.add_cat_var.get(), "fact")
        self.memory_mgr.add_manual_memory(self.base_cid, self.active_track, txt, category=cat)
        self.add_text_var.set("")
        self._refresh_student_tab()
        self._notify_changed()
        self._set_status("✅ 새 장기 기억을 추가했습니다.")

    def _on_edit_confirmed(self) -> None:
        sel = self.lst_confirmed.curselection()
        if not sel or not self.base_cid:
            return
        idx = int(sel[0])
        if idx < 0 or idx >= len(self._confirmed_ids):
            return
        mid = self._confirmed_ids[idx]
        b = self.memory_mgr.get_student_bucket(self.base_cid, self.active_track)
        item = next((m for m in (b.get("confirmed_memories") or []) if m.get("id") == mid), None)
        if item is None:
            return
        edited = simpledialog.askstring(
            "기억 수정",
            "수정할 기억 내용을 입력해 주세요:",
            initialvalue=str(item.get("text") or ""),
            parent=self,
        )
        if edited is not None and edited.strip():
            self.memory_mgr.update_memory_text(self.base_cid, self.active_track, mid, edited.strip())
            self._refresh_student_tab()
            self._notify_changed()
            self._set_status("✏️ 기억 내용을 수정했습니다.")

    def _on_delete_confirmed(self) -> None:
        sel = self.lst_confirmed.curselection()
        if not sel or not self.base_cid:
            return
        idx = int(sel[0])
        if idx < 0 or idx >= len(self._confirmed_ids):
            return
        mid = self._confirmed_ids[idx]
        if self.memory_mgr.delete_memory_item(self.base_cid, self.active_track, mid):
            self._refresh_student_tab()
            self._notify_changed()
            self._set_status("🗑️ 선택한 기억을 삭제했습니다.")

    def _on_approve_pending(self) -> None:
        sel = self.lst_pending.curselection()
        if not sel or not self.base_cid:
            return
        idx = int(sel[0])
        if idx < 0 or idx >= len(self._pending_ids):
            return
        pid = self._pending_ids[idx]
        if self.memory_mgr.approve_pending_memory(self.base_cid, self.active_track, pid):
            self._refresh_student_tab()
            self._notify_changed()
            self._set_status("✅ 보류 기억을 확정 장기 기억으로 승격했습니다.")

    def _on_drop_pending(self) -> None:
        sel = self.lst_pending.curselection()
        if not sel or not self.base_cid:
            return
        idx = int(sel[0])
        if idx < 0 or idx >= len(self._pending_ids):
            return
        pid = self._pending_ids[idx]
        if self.memory_mgr.delete_memory_item(self.base_cid, self.active_track, pid):
            self._refresh_student_tab()
            self._notify_changed()
            self._set_status("✕ 보류 기억을 버렸습니다.")

    def _on_save_threads(self) -> None:
        if not self.base_cid:
            return
        raw = self.txt_threads.get("1.0", tk.END)
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        self.memory_mgr.update_open_threads(self.base_cid, self.active_track, lines)
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
        ok, msg = self.memory_mgr.rollback_to_snapshot(self.base_cid, self.active_track)
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
        self.memory_mgr.clear_student_memory(self.base_cid, self.active_track, include_sessions=include_sessions)
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
        tk.Label(
            self.tab_sensei,
            text="여기에 적어둔 정보는 196명 모든 학생이 공통으로 기억하는 '선생님 기본 프로필'로 활용됩니다.",
            bg="#FFFFFF",
            fg="#475569",
            font=("Malgun Gothic", 9),
            anchor="w",
            justify="left",
        ).pack(fill="x", pady=(0, 12))

        tk.Label(
            self.tab_sensei,
            text="선생님 호칭 / 별명 (비워두면 각 학생의 기본 호칭 사용)",
            bg="#FFFFFF",
            fg="#1E293B",
            font=("Malgun Gothic", 9, "bold"),
            anchor="w",
        ).pack(fill="x")
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

        tk.Label(
            self.tab_sensei,
            text="관심사 · 취향 키워드 (쉼표로 구분 · 예: 아메리카노, 야간 작업, 일본어 공부, 고양이)",
            bg="#FFFFFF",
            fg="#1E293B",
            font=("Malgun Gothic", 9, "bold"),
            anchor="w",
        ).pack(fill="x")
        self.sp_prefs_var = tk.StringVar()
        tk.Entry(
            self.tab_sensei,
            textvariable=self.sp_prefs_var,
            font=("Malgun Gothic", 10),
            bg="#F8FAFC",
            fg="#0F172A",
            relief="solid",
            bd=1,
        ).pack(fill="x", pady=(4, 12))

        tk.Label(
            self.tab_sensei,
            text="선생님 소개 · 생활 리듬 메모 (모든 학생이 알고 있으면 좋은 배경 정보)",
            bg="#FFFFFF",
            fg="#1E293B",
            font=("Malgun Gothic", 9, "bold"),
            anchor="w",
        ).pack(fill="x")
        self.txt_sp_bio = tk.Text(
            self.tab_sensei,
            height=9,
            font=("Malgun Gothic", 10),
            bg="#F8FAFC",
            fg="#0F172A",
            relief="solid",
            bd=1,
            wrap="word",
        )
        self.txt_sp_bio.pack(fill="both", expand=True, pady=(4, 12))

        tk.Button(
            self.tab_sensei,
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
        ).pack(anchor="e")

    def _load_sensei_profile_into_ui(self) -> None:
        sp = self.memory_mgr.load_sensei_profile()
        self.sp_nick_var.set(str(sp.get("nickname") or ""))
        self.sp_prefs_var.set(", ".join(sp.get("preferences") or []))
        self.txt_sp_bio.delete("1.0", tk.END)
        if sp.get("bio_notes"):
            self.txt_sp_bio.insert("1.0", str(sp["bio_notes"]))

    def _on_save_sensei_profile(self) -> None:
        prefs = [p.strip() for p in self.sp_prefs_var.get().split(",") if p.strip()]
        bio = self.txt_sp_bio.get("1.0", tk.END).strip()
        self.memory_mgr.save_sensei_profile(
            nickname=self.sp_nick_var.get(),
            bio_notes=bio,
            preferences=prefs,
        )
        self._notify_changed()
        self._set_status("💾 공통 선생님 프로필을 저장했습니다 (모든 학생에게 반영).")

    # ------------------------------------------------------------------
    # Tab 2: JSON Backup & Restore
    # ------------------------------------------------------------------

    def _build_backup_tab(self) -> None:
        tk.Label(
            self.tab_backup,
            text="모든 대화 기록과 장기 기억은 PC의 개인 데이터 폴더에만 안전하게 격리 저장됩니다.",
            bg="#FFFFFF",
            fg="#475569",
            font=("Malgun Gothic", 9),
            anchor="w",
        ).pack(fill="x", pady=(0, 8))

        path_box = tk.Frame(self.tab_backup, bg="#F8FAFC", padx=10, pady=8, highlightbackground="#E2E8F0", highlightthickness=1)
        path_box.pack(fill="x", pady=(0, 14))
        tk.Label(
            path_box,
            text=str(self.memory_mgr.memory_dir),
            bg="#F8FAFC",
            fg="#334155",
            font=("Consolas", 9),
            anchor="w",
        ).pack(side="left", fill="x", expand=True)
        tk.Button(
            path_box,
            text="📂 폴더 열기",
            command=self._open_memory_folder,
            bg="#E2E8F0",
            fg="#1E293B",
            font=("Malgun Gothic", 8, "bold"),
            relief="flat",
            padx=8,
            pady=3,
            cursor="hand2",
        ).pack(side="right")

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
        tk.Button(
            exp_frame,
            text="📤 전체 학생 기억 · 대화 기록 백업 (JSON)",
            command=lambda: self._on_export_backup(single_student=False),
            bg="#0EA5E9",
            fg="#FFFFFF",
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
        imp_frame.pack(fill="x")
        tk.Button(
            imp_frame,
            text="📥 백업 불러오기 — 기존 기록과 병합 (권장)",
            command=lambda: self._on_import_backup(merge=True),
            bg="#16A34A",
            fg="#FFFFFF",
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
            font=("Malgun Gothic", 9, "bold"),
            relief="flat",
            padx=14,
            pady=5,
            cursor="hand2",
        ).pack(anchor="w")

    def _open_memory_folder(self) -> None:
        folder = self.memory_mgr.memory_dir
        try:
            if os.name == "nt":
                os.startfile(str(folder))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception as exc:
            messagebox.showerror("폴더 열기 실패", str(exc), parent=self)

    def _on_export_backup(self, single_student: bool) -> None:
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
        default_name = (
            f"momotalk_memory_{self.base_cid}_{stamp}.json"
            if (single_student and self.base_cid)
            else f"momotalk_memory_all_{stamp}.json"
        )
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
            stats = self.memory_mgr.export_backup(
                Path(dest),
                base_cid=self.base_cid if single_student else None,
                track=self.active_track if single_student else None,
            )
            self._set_status(
                f"📤 백업 완료: 학생버킷 {stats['buckets']}개 · 기억 {stats['memories']}개 · 통화 {stats['sessions']}건"
            )
        except Exception as exc:
            messagebox.showerror("백업 실패", str(exc), parent=self)

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
            stats = self.memory_mgr.import_backup(Path(src), merge=merge)
            self._refresh_student_tab()
            self._load_sensei_profile_into_ui()
            self._notify_changed()
            self._set_status(
                f"📥 복원 완료: 버킷 {stats['buckets']}개 · 기억 {stats['memories']}개 · 통화 {stats['sessions']}건"
            )
        except Exception as exc:
            messagebox.showerror("복원 실패", str(exc), parent=self)
