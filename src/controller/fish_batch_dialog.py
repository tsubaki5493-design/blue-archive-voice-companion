"""
MomoTalk In-App Full-Automation Dialog for Blue Archive -> Fish Audio Batch Voice Cloning
-----------------------------------------------------------------------------------------
Lets users run both stages directly from the MomoTalk UI with zero command-line typing:
  - Stage 1 (Pre-payment, $0): Pre-extract all 157 JP + 148 KR studio reference WAVs from Steam
  - Sync: Pull existing models already on the user's Fish Audio account into config.json
  - Stage 2 (Post-payment): Resumable auto-upload with live progress bar, safe Stop,
    visibility selector (private/unlist/public), and real-time MomoTalk pink heart updates!
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
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

_PROFILE_CHOICES: list[tuple[str, str]] = [
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
    ) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.characters = characters
        self._api_key_getter = api_key_getter
        self._on_model_linked = on_model_linked
        self._on_finished = on_finished

        self._stop_event = threading.Event()
        self._running = False
        self._closing = False

        self.title("MomoTalk — 전 캐릭터 Fish Audio 일괄 자동화")
        self.configure(bg="#F6F8FB")
        self.geometry("790x700")
        self.minsize(700, 610)
        self.transient(parent)
        self.grab_set()

        self._build_ui()
        self._refresh_inventory_banner()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _current_api_key(self) -> str:
        if callable(self._api_key_getter):
            val = str(self._api_key_getter() or "").strip()
            if val:
                return val
        return str(self.cfg.get("fish_api_key") or "").strip()

    def _build_ui(self) -> None:
        hdr = tk.Frame(self, bg="#FB7390", padx=16, pady=10)
        hdr.pack(fill="x")
        tk.Label(
            hdr,
            text="🚀 전 캐릭터 Fish Audio 보이스 완전 자동화 (JP / KR)",
            bg="#FB7390",
            fg="#FFFFFF",
            font=("Malgun Gothic", 12, "bold"),
        ).pack(side="left")

        body = tk.Frame(self, bg="#F6F8FB", padx=14, pady=10)
        body.pack(fill="both", expand=True)

        # Inventory summary card
        inv_card = tk.Frame(body, bg="#FFFFFF", highlightbackground="#E2E8F0", highlightthickness=1, padx=12, pady=10)
        inv_card.pack(fill="x", pady=(0, 10))
        self.lbl_inventory = tk.Label(
            inv_card,
            text="스팀 음성 팩 현황을 스캔하는 중...",
            bg="#FFFFFF",
            fg="#1E293B",
            font=("Malgun Gothic", 10, "bold"),
            anchor="w",
            justify="left",
        )
        self.lbl_inventory.pack(fill="x")
        tk.Label(
            inv_card,
            text=(
                "💡 팁: 결제 전에 [1단계: 스팀 전 캐릭터 사전 추출]을 미리 돌려두면, "
                "Fish Audio 요금제 결제 후 [2단계: 자동 업로드]만 눌러 바로 💗 모모톡 하트가 채워집니다. "
                "(중단해도 이어서 재개되며 이미 계정에 올라간 모델은 중복 생성하지 않습니다.)"
            ),
            bg="#FFFFFF",
            fg="#64748B",
            font=("Malgun Gothic", 8),
            wraplength=700,
            justify="left",
            anchor="w",
        ).pack(fill="x", pady=(4, 0))

        # Options card
        opt_card = tk.Frame(body, bg="#FFFFFF", highlightbackground="#E2E8F0", highlightthickness=1, padx=12, pady=10)
        opt_card.pack(fill="x", pady=(0, 10))

        row1 = tk.Frame(opt_card, bg="#FFFFFF")
        row1.pack(fill="x", pady=(0, 6))
        tk.Label(row1, text="대상 언어:", bg="#FFFFFF", fg="#334155", font=("Malgun Gothic", 9, "bold"), width=10, anchor="w").pack(side="left")
        self.lang_var = tk.StringVar(value=_LANG_CHOICES[0][1])
        ttk.Combobox(
            row1,
            textvariable=self.lang_var,
            values=[lbl for _, lbl in _LANG_CHOICES],
            state="readonly",
            width=30,
        ).pack(side="left", padx=(0, 14))

        tk.Label(row1, text="업로드 상한:", bg="#FFFFFF", fg="#334155", font=("Malgun Gothic", 9, "bold")).pack(side="left", padx=(0, 4))
        self.limit_var = tk.StringVar(value="전체 (제한 없음)")
        ttk.Combobox(
            row1,
            textvariable=self.limit_var,
            values=["전체 (제한 없음)", "5개 (소량 테스트)", "10개 (Plus 비공개 한도)", "30개", "50개"],
            state="readonly",
            width=20,
        ).pack(side="left")

        row2 = tk.Frame(opt_card, bg="#FFFFFF")
        row2.pack(fill="x", pady=(0, 6))
        tk.Label(row2, text="공개 범위:", bg="#FFFFFF", fg="#334155", font=("Malgun Gothic", 9, "bold"), width=10, anchor="w").pack(side="left")
        self.vis_var = tk.StringVar(value=_VISIBILITY_CHOICES[0][1])
        ttk.Combobox(
            row2,
            textvariable=self.vis_var,
            values=[lbl for _, lbl in _VISIBILITY_CHOICES],
            state="readonly",
            width=54,
        ).pack(side="left", fill="x", expand=True)

        row2b = tk.Frame(opt_card, bg="#FFFFFF")
        row2b.pack(fill="x", pady=(0, 6))
        tk.Label(row2b, text="음질 프리셋:", bg="#FFFFFF", fg="#334155", font=("Malgun Gothic", 9, "bold"), width=10, anchor="w").pack(side="left")
        self.profile_var = tk.StringVar(value=_PROFILE_CHOICES[0][1])
        cmb_prof = ttk.Combobox(
            row2b,
            textvariable=self.profile_var,
            values=[lbl for _, lbl in _PROFILE_CHOICES],
            state="readonly",
            width=54,
        )
        cmb_prof.pack(side="left", fill="x", expand=True)
        cmb_prof.bind("<<ComboboxSelected>>", lambda _e: self._refresh_inventory_banner())

        row3 = tk.Frame(opt_card, bg="#FFFFFF")
        row3.pack(fill="x", pady=(0, 4))
        self.cover_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            row3,
            text="스팀 학생 초상화 썸네일(cover_image) 자동 첨부",
            variable=self.cover_var,
            bg="#FFFFFF",
            fg="#334155",
            font=("Malgun Gothic", 9),
        ).pack(side="left", padx=(0, 14))
        self.sync_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            row3,
            text="업로드 전 내 Fish Audio 계정 기존 모델 자동 동기화 (중복 방지)",
            variable=self.sync_var,
            bg="#FFFFFF",
            fg="#334155",
            font=("Malgun Gothic", 9),
        ).pack(side="left")

        row4 = tk.Frame(opt_card, bg="#FFFFFF")
        row4.pack(fill="x")
        self.replace_external_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            row4,
            text="✨ 이미 링크 있는 학생도 내 계정 모델로 새로 만들기 (기존 외부 링크 자동 백업 후 교체 · 내 모델은 중복 방지)",
            variable=self.replace_external_var,
            command=self._refresh_inventory_banner,
            bg="#FFFFFF",
            fg="#BE185D",
            font=("Malgun Gothic", 9, "bold"),
        ).pack(side="left")

        # Action button strip
        act_row = tk.Frame(body, bg="#F6F8FB")
        act_row.pack(fill="x", pady=(0, 8))

        self.btn_extract = tk.Button(
            act_row,
            text="1️⃣ 스팀 전 캐릭터 사전 추출 (무료)",
            command=lambda: self._start_run("extract_only"),
            bg="#0EA5E9",
            fg="#FFFFFF",
            activebackground="#0284C7",
            font=("Malgun Gothic", 9, "bold"),
            relief="flat",
            padx=12,
            pady=7,
            cursor="hand2",
        )
        self.btn_extract.pack(side="left", padx=(0, 6))

        self.btn_sync = tk.Button(
            act_row,
            text="🔄 기존 계정 모델 동기화",
            command=lambda: self._start_run("sync_only"),
            bg="#475569",
            fg="#FFFFFF",
            activebackground="#334155",
            font=("Malgun Gothic", 9, "bold"),
            relief="flat",
            padx=12,
            pady=7,
            cursor="hand2",
        )
        self.btn_sync.pack(side="left", padx=(0, 6))

        self.btn_upload = tk.Button(
            act_row,
            text="2️⃣ 결제 후 전 캐릭터 자동 업로드",
            command=lambda: self._start_run("upload"),
            bg="#FB7390",
            fg="#FFFFFF",
            activebackground="#E45C7A",
            font=("Malgun Gothic", 9, "bold"),
            relief="flat",
            padx=14,
            pady=7,
            cursor="hand2",
        )
        self.btn_upload.pack(side="left", padx=(0, 6))

        self.btn_stop = tk.Button(
            act_row,
            text="⏹ 안전 중지",
            command=self._request_stop,
            state="disabled",
            bg="#EF4444",
            fg="#FFFFFF",
            disabledforeground="#FCA5A5",
            font=("Malgun Gothic", 9, "bold"),
            relief="flat",
            padx=12,
            pady=7,
            cursor="hand2",
        )
        self.btn_stop.pack(side="right")

        # Progress bar & live log
        prog_row = tk.Frame(body, bg="#F6F8FB")
        prog_row.pack(fill="x", pady=(0, 6))
        self.prog_var = tk.DoubleVar(value=0.0)
        self.progressbar = ttk.Progressbar(prog_row, variable=self.prog_var, maximum=100.0)
        self.progressbar.pack(fill="x")
        self.lbl_progress = tk.Label(
            prog_row,
            text="대기 중",
            bg="#F6F8FB",
            fg="#475569",
            font=("Malgun Gothic", 9, "bold"),
            anchor="w",
        )
        self.lbl_progress.pack(fill="x", pady=(4, 0))

        self.log_box = scrolledtext.ScrolledText(
            body,
            height=13,
            font=("Consolas", 9),
            bg="#0F172A",
            fg="#E2E8F0",
            wrap="word",
            relief="flat",
            bd=0,
            state="disabled",
        )
        self.log_box.pack(fill="both", expand=True)

    def _append_log(self, text: str) -> None:
        if self._closing:
            return
        safe_text = _redact_secrets(str(text or ""), extra_secrets=(self._current_api_key(),))

        def _do() -> None:
            if self._closing or not self.winfo_exists():
                return
            self.log_box.configure(state="normal")
            self.log_box.insert(tk.END, safe_text + "\n")
            self.log_box.see(tk.END)
            self.log_box.configure(state="disabled")

        try:
            self.after(0, _do)
        except Exception:
            pass

    def _selected_quality_profile(self) -> str:
        if hasattr(self, "profile_var"):
            return _PROF_LABEL_TO_CODE.get(self.profile_var.get(), DEFAULT_QUALITY_PROFILE)
        return DEFAULT_QUALITY_PROFILE

    def _refresh_inventory_banner(self) -> None:
        try:
            prof_key = self._selected_quality_profile()
            scan = scan_voiced_roster(
                self.characters,
                self.cfg,
                langs=("JP", "KR"),
                game_dir=None,
                quality_profile=prof_key,
            )
            s = scan["summary"]
            prof_tag = "85초 고품질" if prof_key == "pro85" else "45초 표준"
            ext_jp = s.get("jp_external_linked", 0)
            ext_kr = s.get("kr_external_linked", 0)
            own_jp = s.get("jp_self_owned", 0)
            own_kr = s.get("kr_self_owned", 0)
            replace_ext = bool(self.replace_external_var.get()) if hasattr(self, "replace_external_var") else True
            ext_note = "교체예정" if replace_ext else "유지"
            self.lbl_inventory.configure(
                text=(
                    f"📦 스팀 보이스 ({prof_tag}) — "
                    f"🇯🇵 JP {s['jp_voiced']}명 (추출 {s['jp_extracted']} · 내모델 {own_jp} · 외부링크 {ext_jp} {ext_note})  |  "
                    f"🇰🇷 KR {s['kr_voiced']}명 (추출 {s['kr_extracted']} · 내모델 {own_kr} · 외부링크 {ext_kr} {ext_note})"
                )
            )
        except Exception as exc:
            safe_exc = _redact_secrets(str(exc), extra_secrets=(self._current_api_key(),))
            self.lbl_inventory.configure(text=f"스팀 음성 현황 확인 실패: {safe_exc}")

    def _set_running_ui(self, running: bool) -> None:
        self._running = running
        st = "disabled" if running else "normal"
        self.btn_extract.configure(state=st)
        self.btn_sync.configure(state=st)
        self.btn_upload.configure(state=st)
        self.btn_stop.configure(state="normal" if running else "disabled")

    def _parse_limit(self) -> int | None:
        val = self.limit_var.get()
        for tok in val.split():
            digits = "".join(ch for ch in tok if ch.isdigit())
            if digits:
                n = int(digits)
                return n if n > 0 else None
        return None

    def _start_run(self, mode: str) -> None:
        if self._running:
            return
        api_key = self._current_api_key()
        if mode in ("sync_only", "upload") and not api_key:
            messagebox.showwarning(
                "Fish Audio API Key 필요",
                "먼저 설정 창 1단계에 Fish Audio API Key를 입력해 주세요!\n"
                "(결제 전이라면 [1️⃣ 스팀 전 캐릭터 사전 추출]은 API 키 없이 바로 실행할 수 있습니다.)",
                parent=self,
            )
            return

        langs = _LANG_LABEL_TO_TUPLE.get(self.lang_var.get(), ("JP", "KR"))
        vis = _VIS_LABEL_TO_CODE.get(self.vis_var.get(), "private")
        prof_key = self._selected_quality_profile()
        limit_n = self._parse_limit()

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

        self._stop_event.clear()
        self._set_running_ui(True)
        self.prog_var.set(0.0)
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
                self.prog_var.set(min(100.0, max(0.0, pct)))
                self.lbl_progress.configure(text=summary_txt)

            try:
                self.after(0, _upd)
            except Exception:
                pass

        def _worker() -> None:
            fatal_err_text = ""
            try:
                res = run_fish_batch_pipeline(
                    mode=mode,
                    langs=langs,
                    visibility=vis,
                    quality_profile=prof_key,
                    api_key=api_key,
                    characters=self.characters,
                    cfg=self.cfg,
                    limit=limit_n if mode == "upload" else None,
                    delay_sec=1.5,
                    include_cover=bool(self.cover_var.get()),
                    sync_before_upload=bool(self.sync_var.get()),
                    replace_external=replace_ext,
                    stop_event=self._stop_event,
                    on_progress=_on_prog,
                    on_model_linked=self._on_model_linked,
                    log_cb=self._append_log,
                )
                if not res.get("ok") and res.get("error"):
                    fatal_err_text = _redact_secrets(str(res["error"]), extra_secrets=(api_key,))
            except Exception as exc:
                fatal_err_text = _redact_secrets(f"예기치 못한 오류: {exc}", extra_secrets=(api_key,))
                self._append_log(f"❌ {fatal_err_text}")
            finally:
                def _done_ui() -> None:
                    if self._closing or not self.winfo_exists():
                        return
                    self._set_running_ui(False)
                    self._refresh_inventory_banner()
                    if fatal_err_text:
                        self.lbl_progress.configure(text=f"중단됨: {fatal_err_text[:90]}")
                    if callable(self._on_finished):
                        try:
                            self._on_finished()
                        except Exception:
                            pass

                try:
                    self.after(0, _done_ui)
                except Exception:
                    pass

        threading.Thread(target=_worker, daemon=True, name="fish-batch-ui-worker").start()

    def _request_stop(self) -> None:
        if self._running and not self._stop_event.is_set():
            self._stop_event.set()
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
