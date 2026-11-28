"""
1-Click Google Colab Fish Audio S2-Pro Auto-Connect Launcher
------------------------------------------------------------
Invoked by `2_코랩_실행하기.bat`:
  1. Instant Reuse Check (~0.5s):
     - Pings the saved `colab_s2pro_url` (`/v1/health`).
     - If offline/empty and an explicit `colab_s2pro_drive_folder_id` (or
       `colab_s2pro_endpoint_file` / env var) is configured, checks `live_endpoint.json`
       on Google Drive and pings it.
     - If a live healthy server is found, persists `colab_s2pro_enabled=True` + URL/token
       (DPAPI-encrypted via `save_user_config`) and launches MomoTalk immediately WITHOUT
       opening a browser or showing the waiting window.
  2. Guided Auto-Connect Mode (when server is off):
     - Opens the strictly validated `https://colab.research.google.com/...` notebook URL
       in the user's default browser.
     - Displays a compact, beginner-friendly Korean Tkinter status window showing a 2-step
       checklist, live phase progress from `server_status.json` (polled every 6s using cached
       Drive file IDs -> 1 API call/poll), and a 1-click clipboard bundle paste fallback.
     - As soon as a fresh healthy server is detected, saves config (DPAPI), launches
       MomoTalk cleanly in a detached process, and closes itself.

Security & Clean-Release Guarantees:
  - ZERO hardcoded personal Google Drive IDs, personal folder names, personal paths, or tokens.
  - Reads `colab_s2pro_notebook_id` & `colab_s2pro_drive_folder_id` strictly from user config
    (`%LOCALAPPDATA%\\VoiceCompanionData\\config.json`) or environment variables.
  - Strictly validates Colab notebook URLs (`https://colab.research.google.com/...`) and
    server URLs (SSRF-safe `validate_colab_server_url`) before any network or browser action.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import weakref
import webbrowser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.controller.colab_s2pro_backend import (
    ColabS2ProBackend,
    build_colab_notebook_url,
    discover_endpoint_from_drive,
    parse_colab_connection_bundle,
    read_server_status_from_drive,
    resolve_gdrive_bin,
    validate_colab_notebook_url,
    validate_colab_server_url,
)
from src.utils.app_paths import (
    get_app_root,
    load_user_config,
    redact_secrets,
    save_user_config,
)

POLL_INTERVAL_MS = 6000
CLIPBOARD_WATCH_INTERVAL_MS = 1500
_SAFE_DRIVE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{8,128}$")
_CLIPBOARD_AUTO_HOSTS = (".trycloudflare.com", ".modal.run")

# Phase -> (Beginner-friendly Korean message, progress 0..100)
PHASE_MESSAGES_KR: dict[str, tuple[str, int]] = {
    "init": ("🔄 Colab 서버 초기화를 시작했습니다...", 8),
    "gpu_ok": ("⚡ Colab GPU 할당 완료! 실행 환경을 점검하는 중...", 18),
    "install_deps": ("📦 필수 오디오·추론 라이브러리를 준비하는 중...", 30),
    "install_sglang_omni": ("🚀 SGLang-Omni 고속 추론 엔진을 설치하는 중... (최초 1~2분 소요)", 42),
    "download_weights": ("📥 Fish Audio S2-Pro 음성 모델을 불러오는 중...", 60),
    "merging_lora": ("🎛️ 캐릭터 LoRA 가중치를 병합하는 중...", 70),
    "starting_sglang_omni": ("🔥 SGLang-Omni 엔진 가동 및 CUDA 그래프 워밍업 중... (약 30~60초)", 80),
    "fallback_fish_speech": ("🔁 SGLang-Omni 대체 모드로 Fish-Speech 서버를 준비하는 중...", 76),
    "starting_server": ("🔥 Fish-Speech 서버를 가동하는 중...", 80),
    "starting_tunnel": ("🌐 Cloudflare 보안 터널을 생성하는 중...", 92),
    "ready": ("✅ Colab S2-Pro 서버 준비 완료! 연결을 확인하는 중...", 96),
    "remote_shutdown": ("🛑 이전 서버가 원격 종료되었습니다. Colab에서 [▶ 1단계 셀]을 실행해 주세요.", 0),
    "idle_shutdown": ("💤 이전 서버가 유휴 종료되었습니다. Colab에서 [▶ 1단계 셀]을 실행해 주세요.", 0),
    "stopped": ("⏹️ 이전 서버가 중지 상태입니다. Colab에서 [▶ 1단계 셀]을 실행해 주세요.", 0),
    "error": ("❌ Colab 서버 실행 중 오류가 발생했습니다.", 0),
}

_BOOTING_PHASES = frozenset(
    {
        "init",
        "gpu_ok",
        "install_deps",
        "install_sglang_omni",
        "download_weights",
        "merging_lora",
        "starting_sglang_omni",
        "fallback_fish_speech",
        "starting_server",
        "starting_tunnel",
    }
)


from src.utils.settings_recovery import extract_google_drive_id

_SANITIZE_CHILD_ENV_EXACT = frozenset(
    {
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
        "PYTHONINSPECT",
    }
)


def _build_sanitized_child_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Return a clean child-process environment stripped of Python/pip hijack variables."""
    src_env = dict(os.environ if base_env is None else base_env)
    cleaned: dict[str, str] = {}
    for k, v in src_env.items():
        ku = k.upper()
        if ku in _SANITIZE_CHILD_ENV_EXACT or ku.startswith("PIP_"):
            continue
        cleaned[k] = v
    cleaned["PYTHONNOUSERSITE"] = "1"
    cleaned["PYTHONUTF8"] = "1"
    cleaned["PYTHONIOENCODING"] = "utf-8"
    return cleaned


def _clean_drive_id(raw: Any, *, kind: str = "any") -> str:
    extracted = extract_google_drive_id(raw, kind=kind)  # type: ignore[arg-type]
    if extracted:
        return extracted
    s = str(raw or "").strip()
    return s if (s and _SAFE_DRIVE_ID_RE.match(s)) else ""


def resolve_launcher_drive_settings(cfg: dict[str, Any] | None) -> tuple[str, str, str]:
    """
    Resolve `(notebook_id, drive_folder_id, endpoint_file)` strictly from user config or env vars.
    Never falls back to any personal Drive ID or path.
    """
    c = cfg if isinstance(cfg, dict) else {}
    notebook_id = _clean_drive_id(c.get("colab_s2pro_notebook_id"), kind="notebook") or _clean_drive_id(
        os.environ.get("BA_COLAB_S2PRO_NOTEBOOK_ID", ""),
        kind="notebook",
    )
    drive_folder_id = _clean_drive_id(c.get("colab_s2pro_drive_folder_id"), kind="folder") or _clean_drive_id(
        os.environ.get("BA_COLAB_S2PRO_DRIVE_FOLDER_ID", ""),
        kind="folder",
    )
    endpoint_file = (
        str(c.get("colab_s2pro_endpoint_file") or "").strip()
        or os.environ.get("BA_COLAB_S2PRO_ENDPOINT_FILE", "").strip()
    )
    return notebook_id, drive_folder_id, endpoint_file


def format_server_status_korean(status: dict[str, Any] | None) -> tuple[str, int, str]:
    """Translate `server_status.json` into `(korean_text, progress_pct, phase_code)`."""
    if not isinstance(status, dict) or not status:
        return ("⏳ Colab 1단계 셀 실행을 기다리는 중...", 4, "")
    phase = str(status.get("phase") or "").strip().lower()
    gpu = str(status.get("gpu") or "").strip()
    base_msg, pct = PHASE_MESSAGES_KR.get(
        phase,
        (f"🔄 Colab 서버 상태 확인 중 ({phase or 'unknown'})...", 25),
    )
    if gpu and phase not in ("remote_shutdown", "idle_shutdown", "stopped", "error"):
        msg = f"{base_msg}\n(할당 GPU: {gpu})"
    elif phase == "error":
        raw_err = redact_secrets(str(status.get("error") or "").strip())
        if raw_err:
            detail = raw_err[:240] + ("…" if len(raw_err) > 240 else "")
            msg = f"{base_msg}\n{detail}"
        else:
            msg = f"{base_msg}\nColab 탭의 오류 내용을 확인한 뒤 [▶ 1단계 셀]을 다시 실행해 주세요."
    else:
        msg = base_msg
    return msg, pct, phase


def extract_auto_connect_clipboard_bundle(
    raw_text: str,
    *,
    stale_dead_url: str = "",
    seen_dead_urls: set[str] | frozenset[str] | None = None,
) -> tuple[str, str] | None:
    """
    Inspect clipboard text for a fresh, recognizable Colab S2-Pro connection bundle.
    Accepts only text mentioning a known tunnel host (`trycloudflare.com` / `modal.run`) or a
    `ba_s2pro_` token, validates via `parse_colab_connection_bundle`, and skips stale/dead URLs.
    """
    txt = str(raw_text or "").strip()
    if not txt or len(txt) > 8192:
        return None
    low = txt.lower()
    if not ("trycloudflare.com" in low or "modal.run" in low or "ba_s2pro_" in low):
        return None
    try:
        url, key = parse_colab_connection_bundle(txt)
    except ValueError:
        return None

    norm_url = url.rstrip("/")
    parsed_host = (urlparse(norm_url).hostname or "").lower()
    has_known_host = any(parsed_host.endswith(suf) for suf in _CLIPBOARD_AUTO_HOSTS)
    has_ba_key = key.startswith("ba_s2pro_")
    if not (has_known_host or has_ba_key):
        return None

    dead_set = {str(u).strip().rstrip("/") for u in (seen_dead_urls or ()) if str(u).strip()}
    if stale_dead_url:
        dead_set.add(str(stale_dead_url).strip().rstrip("/"))
    if norm_url in dead_set:
        return None
    return norm_url, key


def open_colab_notebook_in_browser(notebook_id: str | None = None) -> str:
    """Open strictly validated `https://colab.research.google.com/...` URL in default browser."""
    raw_url = build_colab_notebook_url(notebook_id)
    safe_url = validate_colab_notebook_url(raw_url)
    webbrowser.open(safe_url, new=2)
    return safe_url


def persist_connected_endpoint(
    url: str,
    api_key: str,
    *,
    app_root: Path | None = None,
) -> dict[str, Any]:
    """
    Save active Colab S2-Pro URL & API key into `%LOCALAPPDATA%\\VoiceCompanionData\\config.json`
    with DPAPI secret protection and `colab_s2pro_enabled=True`.
    """
    clean_url = validate_colab_server_url(url, resolve_dns=False)
    clean_key = "".join(ch for ch in str(api_key or "").strip() if ord(ch) >= 32 and ord(ch) != 127).strip()
    cfg = load_user_config(app_root=app_root)
    cfg["colab_s2pro_enabled"] = True
    cfg["colab_s2pro_url"] = clean_url
    if clean_key:
        cfg["colab_s2pro_api_key"] = clean_key
    save_user_config(cfg)
    return cfg


def _ping_candidate(url: str, api_key: str, timeout_sec: float = 4.0) -> tuple[bool, float]:
    backend = ColabS2ProBackend(url_getter=lambda: url, api_key_getter=lambda: api_key)
    try:
        return backend.ping_health(timeout=timeout_sec)
    finally:
        backend.close()


def probe_reusable_server(
    cfg: dict[str, Any],
    *,
    timeout_sec: float = 4.0,
    cached_file_ids: dict[str, str] | None = None,
    stale_out: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """
    Fast startup check (~0.5s when saved URL is alive):
      1. Ping saved `colab_s2pro_url` (`/v1/health`).
      2. If offline/empty and Drive folder ID or endpoint file is configured, check `live_endpoint.json`
         on Drive and verify live health.
    Returns connection metadata dict if a healthy server is reachable, else `None`.
    If `stale_out` dict is provided and Drive returned an offline endpoint, populates
    `stale_out["url"]` and `stale_out["updated_at"]` to avoid redundant Drive downloads.
    """
    saved_url = str(cfg.get("colab_s2pro_url") or "").strip()
    saved_key = str(cfg.get("colab_s2pro_api_key") or "").strip()
    if saved_url:
        try:
            norm_url = validate_colab_server_url(saved_url, resolve_dns=False)
            ok, rtt = _ping_candidate(norm_url, saved_key, timeout_sec=timeout_sec)
            if ok:
                return {
                    "source": "saved_config",
                    "url": norm_url,
                    "api_key": saved_key,
                    "rtt_ms": round(rtt, 1),
                }
        except ValueError:
            pass

    _nb_id, folder_id, ep_file = resolve_launcher_drive_settings(cfg)
    if not folder_id and not ep_file:
        return None

    info = discover_endpoint_from_drive(
        timeout_sec=max(6.0, float(timeout_sec)),
        endpoint_file=ep_file or None,
        drive_folder_id=folder_id or None,
        cached_file_ids=cached_file_ids,
    )
    if not info or not info.get("url"):
        return None

    cand_url = str(info.get("url") or "").strip()
    cand_key = str(info.get("api_key") or "").strip() or saved_key
    cand_updated = str(info.get("updated_at") or "").strip()
    ok, rtt = _ping_candidate(cand_url, cand_key, timeout_sec=timeout_sec)
    if not ok:
        if isinstance(stale_out, dict):
            stale_out["url"] = cand_url
            stale_out["updated_at"] = cand_updated
        return None

    return {
        "source": "drive",
        "url": cand_url,
        "api_key": cand_key,
        "gpu": str(info.get("gpu") or "").strip(),
        "updated_at": cand_updated,
        "rtt_ms": round(rtt, 1),
    }


def poll_drive_once(
    *,
    drive_folder_id: str,
    endpoint_file: str = "",
    cached_file_ids: dict[str, str] | None = None,
    stale_dead_url: str = "",
    stale_updated_at: str = "",
    timeout_sec: float = 8.0,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """
    Single Drive poll iteration designed for 6-second polling with minimal Drive API usage:
      - Reads `server_status.json` using cached Drive file ID (1 API call once cached), or
        re-lists the Drive folder when `force_refresh=True` (defeats Drive FUSE `os.replace` churn).
      - During active boot phases (`init`..`starting_tunnel`), skips downloading `live_endpoint.json`.
      - When `phase == "ready"` (or `server_status.json` is unavailable), checks `live_endpoint.json`
        (via cached file ID) and live-pings `/v1/health` before signaling ready!
    """
    cache = dict(cached_file_ids or {})
    status_file: Path | None = None
    if endpoint_file:
        ep_p = Path(endpoint_file)
        sib = ep_p.parent / "server_status.json"
        if sib.is_file() and not sib.is_symlink():
            status_file = sib

    status, cache = read_server_status_from_drive(
        timeout_sec=timeout_sec,
        drive_folder_id=drive_folder_id or None,
        status_file=status_file,
        cached_file_ids=cache,
        force_refresh=force_refresh,
    )
    phase = str((status or {}).get("phase") or "").strip().lower()
    status_updated = str((status or {}).get("updated_at") or "").strip()
    status_url = str((status or {}).get("url") or "").strip()

    # If server is clearly mid-boot or explicitly stopped, skip extra endpoint download (1 call total!)
    if phase in _BOOTING_PHASES or phase in ("remote_shutdown", "idle_shutdown", "stopped", "error"):
        return {
            "ready": False,
            "stale_session": False,
            "status": status,
            "cached_file_ids": cache,
        }

    # If server_status still shows an old dead URL from before user ran Cell 1, avoid showing false "ready"
    # Require non-empty `stale_updated_at` so reused/fixed/localhost URLs after clean shutdown never deadlock!
    if (
        phase == "ready"
        and stale_dead_url
        and status_url
        and status_url.rstrip("/") == stale_dead_url.rstrip("/")
        and bool(stale_updated_at)
        and ((not status_updated) or status_updated <= stale_updated_at)
    ):
        return {
            "ready": False,
            "stale_session": True,
            "status": status,
            "cached_file_ids": cache,
        }

    # If server_status says ready (or status file missing), inspect live_endpoint.json + live health check.
    # If `read_server_status_from_drive` already ran `force_refresh` via Drive ls, `cache` already holds
    # fresh file IDs (no need to run a duplicate `ls`); otherwise pass `force_refresh` through.
    ep_force_refresh = bool(force_refresh and (status_file is not None))
    info = discover_endpoint_from_drive(
        timeout_sec=timeout_sec,
        endpoint_file=endpoint_file or None,
        drive_folder_id=drive_folder_id or None,
        cached_file_ids=cache,
        force_refresh=ep_force_refresh,
    )
    stale_hit = False
    if info and info.get("url"):
        cand_url = str(info.get("url") or "").strip()
        cand_key = str(info.get("api_key") or "").strip()
        cand_updated = str(info.get("updated_at") or status_updated or "").strip()
        same_as_stale_url = bool(
            stale_dead_url and cand_url.rstrip("/") == stale_dead_url.rstrip("/")
        )
        # Avoid re-pinging a known dead stale endpoint unless its timestamp or URL actually refreshed
        is_same_stale = bool(
            same_as_stale_url
            and stale_updated_at
            and cand_updated
            and cand_updated <= stale_updated_at
        )
        if is_same_stale:
            stale_hit = True
        else:
            ok, rtt = _ping_candidate(cand_url, cand_key, timeout_sec=min(5.0, float(timeout_sec)))
            if ok:
                return {
                    "ready": True,
                    "stale_session": False,
                    "url": cand_url,
                    "api_key": cand_key,
                    "gpu": str(info.get("gpu") or (status or {}).get("gpu") or "").strip(),
                    "updated_at": cand_updated,
                    "rtt_ms": round(rtt, 1),
                    "status": status,
                    "cached_file_ids": cache,
                }
            # Only treat failed ping as a stale dead session if this URL IS the previous dead URL;
            # a brand-new fresh tunnel failing its first ping is still warming up / propagating DNS.
            stale_hit = bool(phase == "ready" and same_as_stale_url)
    elif phase == "ready":
        stale_hit = True

    return {
        "ready": False,
        "stale_session": stale_hit,
        "status": status,
        "cached_file_ids": cache,
    }


def launch_momotalk_app(
    app_root: Path | None = None,
    extra_args: list[str] | None = None,
) -> subprocess.Popen[Any]:
    """Launch MomoTalk (`src/controller/aru_fish_bridge.py`) cleanly in a detached process."""
    root = (app_root or get_app_root()).resolve()
    target = (root / "src" / "controller" / "aru_fish_bridge.py").resolve()
    if not target.is_file() or not target.is_relative_to(root):
        raise FileNotFoundError(f"MomoTalk 실행 스크립트를 찾을 수 없습니다: {target}")

    venv_pyw = root / ".venv" / "Scripts" / "pythonw.exe"
    venv_py = root / ".venv" / "Scripts" / "python.exe"
    if venv_pyw.is_file():
        py_exe = venv_pyw
    elif venv_py.is_file():
        py_exe = venv_py
    else:
        py_exe = Path(sys.executable)

    cmd = [str(py_exe), str(target)]
    if extra_args:
        cmd.extend(str(a) for a in extra_args)

    cflags = 0
    if os.name == "nt":
        cflags = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )
    return subprocess.Popen(
        cmd,
        cwd=str(root),
        env=_build_sanitized_child_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=cflags,
        close_fds=True,
    )


class ColabS2ProLauncherWindow:
    """Compact beginner-friendly Korean Tkinter guided auto-connect launcher window."""

    def __init__(
        self,
        *,
        cfg: dict[str, Any],
        app_root: Path | None = None,
        cached_file_ids: dict[str, str] | None = None,
        stale_dead_url: str = "",
        stale_updated_at: str = "",
        no_launch: bool = False,
        no_browser: bool = False,
    ) -> None:
        import tkinter as tk
        from tkinter import ttk

        self._tk = tk
        self._ttk = ttk
        self.cfg = dict(cfg)
        self.app_root = app_root or get_app_root()
        self.notebook_id, self.drive_folder_id, self.endpoint_file = resolve_launcher_drive_settings(self.cfg)
        self._cached_file_ids: dict[str, str] = dict(cached_file_ids or {})
        self._stale_dead_url = stale_dead_url
        self._stale_updated_at = stale_updated_at
        self.no_launch = bool(no_launch)
        self.no_browser = bool(no_browser)
        self._closing = False
        self._closing_ev = threading.Event()
        self._connected = False
        self._poll_inflight = False
        self._poll_count = 0
        self._last_status_updated_at = ""
        self._stalled_poll_streak = 0
        self._force_next_drive_refresh = False
        self._manual_check_inflight = False
        self._started_mono = time.monotonic()
        self._open_nb_after_id: str | None = None
        self._poll_after_id: str | None = None
        self._timer_after_id: str | None = None
        self._clip_after_id: str | None = None
        self._ui_drain_after_id: str | None = None
        self._close_after_id: str | None = None
        self._ui_queue: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self._last_clipboard_text: str = ""
        self._failed_manual_urls: set[str] = set()
        self._can_drive_poll = bool(
            self.endpoint_file or (self.drive_folder_id and resolve_gdrive_bin())
        )

        self.root = tk.Tk()
        self.root.title("☁️ Colab S2-Pro 자동 연결 런처 — MomoTalk Live")
        self.root.geometry("560x560")
        self.root.minsize(540, 540)
        self.root.configure(bg="#F3F5F8")
        self.root.resizable(False, True)

        ico_path = self.app_root / "config" / "aru_bridge_icon.ico"
        if ico_path.is_file():
            try:
                self.root.iconbitmap(str(ico_path))
            except Exception:
                pass

        init_pct = 5 if self._can_drive_poll else 0
        self.status_var = tk.StringVar(
            value=(
                "🌐 Colab 노트북을 열었습니다. 1단계 셀을 실행하면 자동으로 연결됩니다..."
                if self._can_drive_poll
                else "📋 Colab 1단계 셀 완료 후 출력된 1줄 연결 정보를 복사하면 자동 감지되거나, 아래 입력 칸에 붙여넣어 연결할 수 있습니다."
            )
        )
        self.detail_var = tk.StringVar(value="경과 시간: 00:00")
        self.progress_var = tk.DoubleVar(value=float(init_pct))
        self.progress_pct_var = tk.StringVar(value=f"진행률: {init_pct}%")
        self.manual_entry_var = tk.StringVar(value="")

        self._build_ui()
        self._center_window()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        if not self.no_browser:
            self._open_nb_after_id = self.root.after(150, self._open_notebook_safe)
        self._ui_drain_after_id = self.root.after(50, self._drain_ui_queue)
        self._tick_elapsed_timer()
        if self._can_drive_poll:
            self._poll_after_id = self.root.after(1200, self._schedule_drive_poll)
        self._clip_after_id = self.root.after(CLIPBOARD_WATCH_INTERVAL_MS, self._schedule_clipboard_watch)

    def _enqueue_ui(self, fn: Callable[[], None]) -> None:
        """Thread-safe handoff from background worker threads to the Tk main thread."""
        if not self._closing:
            self._ui_queue.put(fn)

    def _drain_ui_queue(self) -> None:
        if self._closing:
            return
        while True:
            try:
                fn = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                if not self._closing:
                    fn()
            except Exception:
                pass
        if not self._closing:
            try:
                self._ui_drain_after_id = self.root.after(50, self._drain_ui_queue)
            except Exception:
                pass

    def _center_window(self) -> None:
        try:
            self.root.update_idletasks()
            w = self.root.winfo_width() or 560
            h = self.root.winfo_height() or 560
            sw = self.root.winfo_screenwidth() or 1280
            sh = self.root.winfo_screenheight() or 720
            x = max(20, (sw - w) // 2)
            y = max(20, (sh - h) // 2 - 20)
            self.root.geometry(f"{w}x{h}+{x}+{y}")
        except Exception:
            pass

    def _set_status(self, text: str, *, level: str = "info") -> None:
        color_map = {
            "info": "#0F766E",
            "ok": "#15803D",
            "warn": "#B45309",
            "error": "#B91C1C",
        }
        self.status_var.set(text)
        lbl = getattr(self, "lbl_status", None)
        if lbl is not None:
            try:
                lbl.configure(fg=color_map.get(level, "#0F766E"))
            except Exception:
                pass

    def _set_progress(self, pct: float) -> None:
        clamped = max(0, min(100, int(round(float(pct)))))
        self.progress_var.set(float(clamped))
        self.progress_pct_var.set(f"진행률: {clamped}%")

    def _set_manual_entry_text(self, raw_text: str) -> None:
        one_line = re.sub(r"[\r\n]+", " ", str(raw_text or "")).strip()
        self.manual_entry_var.set(one_line)
        ent = getattr(self, "entry_manual", None)
        if ent is not None:
            try:
                ent.icursor("end")
            except Exception:
                pass

    def _focus_manual_entry(self) -> None:
        ent = getattr(self, "entry_manual", None)
        if ent is not None:
            try:
                ent.focus_set()
                ent.icursor("end")
            except Exception:
                pass

    def _read_clipboard_text(self, *, allow_pyperclip_fallback: bool = True) -> str:
        try:
            raw = self.root.clipboard_get()
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
        except Exception:
            pass
        if allow_pyperclip_fallback:
            try:
                import pyperclip

                raw2 = pyperclip.paste()
                if isinstance(raw2, str) and raw2.strip():
                    return raw2.strip()
            except Exception:
                pass
        return ""

    def _build_ui(self) -> None:
        tk = self._tk
        ttk = self._ttk

        # MomoTalk coral-pink header bar
        hdr = tk.Frame(self.root, bg="#FB7390", padx=16, pady=10)
        hdr.pack(fill="x")
        tk.Frame(self.root, bg="#E45C7A", height=2).pack(fill="x")
        tk.Label(
            hdr,
            text="☁️ Colab S2-Pro 자동 연결 런처",
            bg="#FB7390",
            fg="#FFFFFF",
            font=("Malgun Gothic", 13, "bold"),
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            hdr,
            text="Colab GPU 서버가 준비되면 주소·토큰 복사 없이 MomoTalk Live를 자동으로 실행합니다",
            bg="#FB7390",
            fg="#FFF1F4",
            font=("Malgun Gothic", 8),
            anchor="w",
        ).pack(fill="x", pady=(2, 0))

        body = tk.Frame(self.root, bg="#F3F5F8", padx=16, pady=10)
        body.pack(fill="both", expand=True)

        # 2-Step Checklist Card
        step_card = tk.Frame(
            body,
            bg="#FFFFFF",
            highlightbackground="#DCE3EC",
            highlightthickness=1,
            padx=14,
            pady=10,
        )
        step_card.pack(fill="x", pady=(0, 8))
        tk.Label(
            step_card,
            text="✅ 딱 2번만 클릭해 주세요!",
            bg="#FFFFFF",
            fg="#0F172A",
            font=("Malgun Gothic", 10, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(0, 5))
        tk.Label(
            step_card,
            text="① 열린 Colab 탭에서  [▶ 1단계 셀 실행]  클릭",
            bg="#FFFFFF",
            fg="#0369A1",
            font=("Malgun Gothic", 10, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(0, 2))
        tk.Label(
            step_card,
            text="② 팝업이 뜨면  [Google Drive에 연결 → 허용]  클릭",
            bg="#FFFFFF",
            fg="#0369A1",
            font=("Malgun Gothic", 10, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(0, 6))
        guide_sub = (
            "💡 Drive 자동 감지가 켜져 있습니다! 주소·토큰 복사 없이 자동으로 연결되고 모모톡이 열립니다."
            if self._can_drive_poll
            else "ℹ️ Drive 자동 감지가 꺼져 있습니다. Colab 출력의 1줄 연결 정보를 복사(Ctrl+C)하거나 아래 입력 칸에 붙여넣어 연결해 주세요!"
        )
        tk.Label(
            step_card,
            text=guide_sub,
            bg="#F0FDF4" if self._can_drive_poll else "#FEF3C7",
            fg="#166534" if self._can_drive_poll else "#92400E",
            font=("Malgun Gothic", 8, "bold"),
            anchor="w",
            justify="left",
            wraplength=490,
            padx=8,
            pady=5,
        ).pack(fill="x")

        # Live Status Card (Progress Bar explicitly labeled so it is never mistaken for an input)
        st_card = tk.Frame(
            body,
            bg="#FFFFFF",
            highlightbackground="#DCE3EC",
            highlightthickness=1,
            padx=14,
            pady=10,
        )
        st_card.pack(fill="x", pady=(0, 8))
        st_hdr = tk.Frame(st_card, bg="#FFFFFF")
        st_hdr.pack(fill="x", pady=(0, 4))
        tk.Label(
            st_hdr,
            text="📡 실시간 서버 상태",
            bg="#FFFFFF",
            fg="#334155",
            font=("Malgun Gothic", 9, "bold"),
            anchor="w",
        ).pack(side="left")
        self.lbl_progress_pct = tk.Label(
            st_hdr,
            textvariable=self.progress_pct_var,
            bg="#FFFFFF",
            fg="#0369A1",
            font=("Malgun Gothic", 9, "bold"),
            anchor="e",
        )
        self.lbl_progress_pct.pack(side="right")

        self.lbl_status = tk.Label(
            st_card,
            textvariable=self.status_var,
            bg="#FFFFFF",
            fg="#0F766E",
            font=("Malgun Gothic", 9, "bold"),
            anchor="w",
            justify="left",
            wraplength=490,
        )
        self.lbl_status.pack(fill="x", pady=(0, 8))

        self.progress_bar = ttk.Progressbar(
            st_card,
            variable=self.progress_var,
            maximum=100.0,
            mode="determinate",
        )
        self.progress_bar.pack(fill="x", pady=(0, 5))

        tk.Label(
            st_card,
            textvariable=self.detail_var,
            bg="#FFFFFF",
            fg="#64748B",
            font=("Malgun Gothic", 8),
            anchor="w",
        ).pack(fill="x")

        # Manual Connection Card (Crisp White Editable tk.Entry + Instant Connect)
        manual_card = tk.Frame(
            body,
            bg="#FFFFFF",
            highlightbackground="#CBD5E1",
            highlightthickness=1,
            padx=14,
            pady=10,
        )
        manual_card.pack(fill="x", pady=(0, 10))
        tk.Label(
            manual_card,
            text="🔗 수동 연결 (Colab 출력 1줄 JSON 또는 https:// 주소 입력/Ctrl+V 후 Enter)",
            bg="#FFFFFF",
            fg="#0F172A",
            font=("Malgun Gothic", 9, "bold"),
            anchor="w",
            justify="left",
            wraplength=490,
        ).pack(fill="x", pady=(0, 6))

        entry_row = tk.Frame(manual_card, bg="#FFFFFF")
        entry_row.pack(fill="x", pady=(0, 4))
        self.entry_manual = tk.Entry(
            entry_row,
            textvariable=self.manual_entry_var,
            bg="#FFFFFF",
            fg="#0F172A",
            insertbackground="#0F172A",
            disabledbackground="#F8FAFC",
            state="normal",
            relief="solid",
            bd=1,
            highlightthickness=1,
            highlightbackground="#94A3B8",
            highlightcolor="#0284C7",
            font=("Malgun Gothic", 9),
        )
        self.entry_manual.pack(side="left", fill="x", expand=True, ipady=5)
        self.entry_manual.bind("<Return>", self._on_manual_connect)
        self.entry_manual.bind("<KP_Enter>", self._on_manual_connect)

        self.btn_connect = tk.Button(
            entry_row,
            text="⚡ 바로 연결",
            command=self._on_manual_connect,
            bg="#0284C7",
            fg="#FFFFFF",
            activebackground="#0369A1",
            activeforeground="#FFFFFF",
            font=("Malgun Gothic", 9, "bold"),
            relief="flat",
            padx=12,
            pady=5,
            cursor="hand2",
        )
        self.btn_connect.pack(side="right", padx=(8, 0))

        tk.Label(
            manual_card,
            text="💡 Colab 출력의 1줄 연결 정보(JSON/URL)를 복사(Ctrl+C)하면 1.5초 안에 자동 감지되어 바로 연결됩니다.",
            bg="#FFFFFF",
            fg="#64748B",
            font=("Malgun Gothic", 8),
            anchor="w",
            justify="left",
            wraplength=490,
        ).pack(fill="x")

        # Action Buttons
        btn_row = tk.Frame(body, bg="#F3F5F8")
        btn_row.pack(fill="x", pady=(2, 0))
        self.btn_paste = tk.Button(
            btn_row,
            text="📋 클립보드에서 넣기",
            command=self._on_paste_bundle,
            bg="#FEF3C7",
            fg="#92400E",
            activebackground="#FDE68A",
            font=("Malgun Gothic", 9, "bold"),
            relief="flat",
            padx=10,
            pady=7,
            cursor="hand2",
        )
        self.btn_paste.pack(side="left", padx=(0, 6))

        self.btn_reopen = tk.Button(
            btn_row,
            text="🌐 노트북 다시 열기",
            command=self._open_notebook_safe,
            bg="#E0F2FE",
            fg="#0369A1",
            activebackground="#BAE6FD",
            font=("Malgun Gothic", 9, "bold"),
            relief="flat",
            padx=10,
            pady=7,
            cursor="hand2",
        )
        self.btn_reopen.pack(side="left", padx=(0, 6))

        self.btn_skip = tk.Button(
            btn_row,
            text="⏭️ 연결 없이 바로 앱 열기",
            command=self._on_skip_and_launch,
            bg="#E2E8F0",
            fg="#334155",
            activebackground="#CBD5E1",
            font=("Malgun Gothic", 9, "bold"),
            relief="flat",
            padx=10,
            pady=7,
            cursor="hand2",
        )
        self.btn_skip.pack(side="right")

    def _open_notebook_safe(self) -> None:
        self._open_nb_after_id = None
        if self._closing or self._connected:
            return
        try:
            open_colab_notebook_in_browser(self.notebook_id or None)
        except Exception as exc:
            if not self._closing:
                self._set_status(f"⚠️ 노트북 열기 실패: {redact_secrets(str(exc))[:140]}", level="warn")

    def _tick_elapsed_timer(self) -> None:
        if self._closing or self._connected:
            return
        elapsed = int(max(0.0, time.monotonic() - self._started_mono))
        mm, ss = divmod(elapsed, 60)
        mode_hint = (
            "Drive 6초 · 클립보드 자동 감지 중"
            if self._can_drive_poll
            else "클립보드 자동 감지 · 수동 입력 대기 중"
        )
        self.detail_var.set(f"경과 시간: {mm:02d}:{ss:02d}  ·  {mode_hint}")
        try:
            self._timer_after_id = self.root.after(1000, self._tick_elapsed_timer)
        except Exception:
            pass

    def _schedule_clipboard_watch(self) -> None:
        if self._closing or self._connected:
            return
        try:
            self._poll_clipboard_once()
        except Exception:
            pass
        if not self._closing and not self._connected:
            try:
                self._clip_after_id = self.root.after(CLIPBOARD_WATCH_INTERVAL_MS, self._schedule_clipboard_watch)
            except Exception:
                pass

    def _poll_clipboard_once(self) -> bool:
        """Check clipboard once for a fresh Colab S2-Pro bundle and auto-connect if valid."""
        if self._closing or self._connected or self._manual_check_inflight:
            return False
        raw_clip = self._read_clipboard_text(allow_pyperclip_fallback=False)
        if not raw_clip or raw_clip == self._last_clipboard_text:
            return False
        self._last_clipboard_text = raw_clip

        parsed = extract_auto_connect_clipboard_bundle(
            raw_clip,
            stale_dead_url=self._stale_dead_url,
            seen_dead_urls=self._failed_manual_urls,
        )
        if parsed is None:
            return False

        self._set_manual_entry_text(raw_clip)
        self._start_bundle_connect(parsed[0], parsed[1], source="auto_clipboard")
        return True

    def _schedule_drive_poll(self) -> None:
        if self._closing or self._connected or not self._can_drive_poll:
            return
        if self._poll_inflight:
            self._poll_after_id = self.root.after(POLL_INTERVAL_MS, self._schedule_drive_poll)
            return
        self._poll_inflight = True
        self._poll_count += 1
        # Periodically force-refresh cached Drive file IDs (every 3 polls / ~18s) AND whenever
        # the previous poll stalled or looked stale/stopped, defeating Drive FUSE file-ID churn.
        should_force_refresh = bool(self._force_next_drive_refresh or (self._poll_count % 3 == 0))
        self._force_next_drive_refresh = False
        cache_snapshot = dict(self._cached_file_ids)
        drive_folder_id = self.drive_folder_id
        endpoint_file = self.endpoint_file
        stale_dead_url = self._stale_dead_url
        stale_updated_at = self._stale_updated_at
        closing_ev = self._closing_ev
        ui_queue = self._ui_queue
        self_ref = weakref.ref(self)

        def _worker() -> None:
            res: dict[str, Any] = {"ready": False, "status": None, "cached_file_ids": cache_snapshot}
            try:
                res = poll_drive_once(
                    drive_folder_id=drive_folder_id,
                    endpoint_file=endpoint_file,
                    cached_file_ids=cache_snapshot,
                    stale_dead_url=stale_dead_url,
                    stale_updated_at=stale_updated_at,
                    timeout_sec=8.0,
                    force_refresh=should_force_refresh,
                )
            except Exception:
                pass
            finally:
                if not closing_ev.is_set():
                    def _deliver(r: dict[str, Any] = res) -> None:
                        win = self_ref()
                        if win is not None and not win._closing:
                            win._apply_poll_result(r)

                    ui_queue.put(_deliver)

        threading.Thread(target=_worker, daemon=True, name="colab-launcher-poll").start()

    def _apply_poll_result(self, res: dict[str, Any]) -> None:
        self._poll_inflight = False
        if self._closing or self._connected:
            return
        new_cache = res.get("cached_file_ids")
        if isinstance(new_cache, dict):
            self._cached_file_ids = dict(new_cache)

        if res.get("ready") and res.get("url"):
            self._finish_with_connection(
                str(res["url"]),
                str(res.get("api_key") or ""),
                gpu=str(res.get("gpu") or ""),
            )
            return

        # Detect stalled timestamps or stale/stopped statuses while waiting for a newly launched server
        # so the next poll forces a Drive `ls` refresh instead of reading a frozen trashed file ID.
        status = res.get("status")
        cur_phase = str(status.get("phase") or "").strip().lower() if isinstance(status, dict) else ""
        cur_updated = str(status.get("updated_at") or "").strip() if isinstance(status, dict) else ""
        if cur_updated and cur_updated == self._last_status_updated_at:
            self._stalled_poll_streak += 1
        else:
            self._stalled_poll_streak = 0
        self._last_status_updated_at = cur_updated

        if (
            res.get("stale_session")
            or self._stalled_poll_streak >= 1
            or (not isinstance(status, dict))
            or (not status)
            or cur_phase in ("remote_shutdown", "idle_shutdown", "stopped", "error")
        ):
            self._force_next_drive_refresh = True

        # Do not clobber in-progress manual/clipboard connection check feedback
        if not self._manual_check_inflight:
            if res.get("stale_session"):
                self._set_status(
                    "⏳ 이전 Colab 서버가 종료되었습니다. 열린 Colab 탭에서 [▶ 1단계 셀 실행]을 클릭해 주세요...",
                    level="warn",
                )
                self._set_progress(5.0)
            elif isinstance(status, dict) and status:
                msg_kr, pct, phase = format_server_status_korean(status)
                if phase == "error":
                    lvl = "error"
                elif phase in ("remote_shutdown", "idle_shutdown", "stopped"):
                    lvl = "warn"
                elif phase == "ready":
                    lvl = "ok"
                else:
                    lvl = "info"
                self._set_status(msg_kr, level=lvl)
                self._set_progress(float(pct))

        if self._can_drive_poll:
            if self._poll_after_id:
                try:
                    self.root.after_cancel(self._poll_after_id)
                except Exception:
                    pass
            try:
                self._poll_after_id = self.root.after(POLL_INTERVAL_MS, self._schedule_drive_poll)
            except Exception:
                pass

    def _on_manual_connect(self, _event: Any = None) -> None:
        if self._closing or self._connected or self._manual_check_inflight:
            return
        raw_text = str(self.manual_entry_var.get() or "").strip()
        if not raw_text:
            self._set_status(
                "⚠️ 아래 입력 칸에 Colab 1줄 연결 정보(JSON) 또는 https:// 주소를 입력/붙여넣기(Ctrl+V)해 주세요.",
                level="warn",
            )
            self._focus_manual_entry()
            return
        try:
            parsed_url, parsed_key = parse_colab_connection_bundle(raw_text)
        except ValueError as exc:
            self._set_status(f"⚠️ {exc}", level="warn")
            self._focus_manual_entry()
            return
        self._start_bundle_connect(parsed_url, parsed_key, source="manual")

    def _on_paste_bundle(self) -> None:
        if self._closing or self._connected or self._manual_check_inflight:
            return
        raw_clip = self._read_clipboard_text()
        if not raw_clip:
            self._set_status(
                "⚠️ 클립보드가 비어 있습니다. Colab에서 연결 정보를 복사(Ctrl+C)하거나 아래 입력 칸에 직접 입력해 주세요.",
                level="warn",
            )
            self._focus_manual_entry()
            return

        self._last_clipboard_text = raw_clip
        self._set_manual_entry_text(raw_clip)
        self._focus_manual_entry()

        try:
            parsed_url, parsed_key = parse_colab_connection_bundle(raw_clip)
        except ValueError as exc:
            self._set_status(f"⚠️ {exc}", level="warn")
            return

        self._start_bundle_connect(parsed_url, parsed_key, source="clipboard")

    def _start_bundle_connect(self, parsed_url: str, parsed_key: str, *, source: str = "manual") -> None:
        if self._closing or self._connected or self._manual_check_inflight:
            return
        self._manual_check_inflight = True
        if source == "auto_clipboard":
            self._set_status("✨ 클립보드에서 새 Colab S2-Pro 연결 정보를 감지했습니다! 연결 확인 중...", level="info")
        else:
            self._set_status("🧪 입력한 Colab S2-Pro 서버 연결 상태를 확인하는 중...", level="info")
        self._set_progress(88.0)
        for btn in (getattr(self, "btn_paste", None), getattr(self, "btn_connect", None)):
            if btn is not None:
                try:
                    btn.configure(state="disabled")
                except Exception:
                    pass

        closing_ev = self._closing_ev
        ui_queue = self._ui_queue
        self_ref = weakref.ref(self)

        def _worker() -> None:
            ok = False
            msg = ""
            backend = ColabS2ProBackend(url_getter=lambda: parsed_url, api_key_getter=lambda: parsed_key)
            try:
                ok, msg, _rtt, _refs = backend.check_health(timeout=10.0)
            except Exception as exc:
                ok = False
                msg = redact_secrets(str(exc), extra_secrets=(parsed_key,))[:160]
            finally:
                backend.close()

            if closing_ev.is_set():
                return

            def _apply() -> None:
                win = self_ref()
                if win is None:
                    return
                win._manual_check_inflight = False
                if win._closing or win._connected:
                    return
                for btn in (getattr(win, "btn_paste", None), getattr(win, "btn_connect", None)):
                    if btn is not None:
                        try:
                            btn.configure(state="normal")
                        except Exception:
                            pass
                if ok:
                    win._finish_with_connection(parsed_url, parsed_key)
                else:
                    win._failed_manual_urls.add(parsed_url.rstrip("/"))
                    win._set_progress(20.0)
                    if source == "auto_clipboard":
                        win._set_status(
                            f"⚠️ 클립보드 주소 연결 실패 ({msg}). 새 Colab 출력 정보를 복사하거나 아래 칸에 입력해 주세요.",
                            level="warn",
                        )
                    else:
                        win._set_status(f"❌ 연결 실패: {msg}", level="error")
                        win._focus_manual_entry()

            ui_queue.put(_apply)

        threading.Thread(target=_worker, daemon=True, name="colab-launcher-paste-check").start()

    def _finish_with_connection(self, url: str, api_key: str, *, gpu: str = "") -> None:
        if self._connected or self._closing:
            return
        self._connected = True
        try:
            persist_connected_endpoint(url, api_key, app_root=self.app_root)
        except Exception as exc:
            self._connected = False
            self._set_status(
                f"❌ 설정 저장 실패: {redact_secrets(str(exc), extra_secrets=(api_key,))[:140]}",
                level="error",
            )
            return

        gpu_note = f" ({gpu})" if gpu else ""
        self._set_progress(100.0)
        self._set_status(f"🎉 Colab S2-Pro 연결 완료{gpu_note}! MomoTalk Live를 실행합니다...", level="ok")
        if not self.no_launch:
            try:
                launch_momotalk_app(app_root=self.app_root, extra_args=["--colab-s2pro"])
            except Exception as exc:
                self._set_status(
                    f"⚠️ 설정 저장 완료, 앱 자동 실행 실패: {redact_secrets(str(exc))[:140]}",
                    level="warn",
                )
                return
        self._close_after_id = self.root.after(550, self._on_close)

    def _on_skip_and_launch(self) -> None:
        if not self.no_launch:
            try:
                launch_momotalk_app(app_root=self.app_root)
            except Exception as exc:
                self._set_status(f"⚠️ MomoTalk 실행 실패: {redact_secrets(str(exc))[:140]}", level="warn")
                return
        self._on_close()

    def _on_close(self) -> None:
        self._closing = True
        self._closing_ev.set()
        for aid in (
            self._open_nb_after_id,
            self._poll_after_id,
            self._timer_after_id,
            self._clip_after_id,
            self._ui_drain_after_id,
            self._close_after_id,
        ):
            if aid:
                try:
                    self.root.after_cancel(aid)
                except Exception:
                    pass
        while True:
            try:
                self._ui_queue.get_nowait()
            except queue.Empty:
                break
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self) -> int:
        self.root.mainloop()
        return 0


def run_launcher(
    argv: list[str] | None = None,
    *,
    app_root: Path | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Colab S2-Pro 1-Click Auto-Connect Launcher for MomoTalk")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Check saved/Drive server health once and exit (0=ready, 1=offline) without UI or launching app.",
    )
    parser.add_argument(
        "--no-launch",
        action="store_true",
        help="Do not launch MomoTalk (aru_fish_bridge.py) after connecting (testing/dry-run).",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not auto-open the Colab notebook browser tab when entering Guided Mode.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=4.0,
        help="Initial health probe timeout in seconds (default: 4.0).",
    )
    args = parser.parse_args(argv)

    root = app_root or get_app_root()
    cfg = load_user_config(app_root=root)
    cached_ids: dict[str, str] = {}
    stale_out: dict[str, str] = {}

    found = probe_reusable_server(
        cfg,
        timeout_sec=max(0.5, float(args.timeout)),
        cached_file_ids=cached_ids,
        stale_out=stale_out,
    )
    if found is not None:
        persist_connected_endpoint(found["url"], found.get("api_key", ""), app_root=root)
        if args.check_only:
            safe_out = {
                "ready": True,
                "source": found.get("source", ""),
                "url": found.get("url", ""),
                "gpu": found.get("gpu", ""),
                "rtt_ms": found.get("rtt_ms", 0.0),
            }
            print(json.dumps(safe_out, ensure_ascii=False))
            return 0
        if not args.no_launch:
            launch_momotalk_app(app_root=root, extra_args=["--colab-s2pro"])
        return 0

    if args.check_only:
        print(json.dumps({"ready": False, "source": "offline"}, ensure_ascii=False))
        return 1

    # Capture stale URL/timestamp already observed during probe without extra Drive API call
    stale_dead_url = stale_out.get("url") or str(cfg.get("colab_s2pro_url") or "").strip()
    stale_updated_at = stale_out.get("updated_at") or ""

    win = ColabS2ProLauncherWindow(
        cfg=cfg,
        app_root=root,
        cached_file_ids=cached_ids,
        stale_dead_url=stale_dead_url,
        stale_updated_at=stale_updated_at,
        no_launch=bool(args.no_launch),
        no_browser=bool(args.no_browser),
    )
    return win.run()


if __name__ == "__main__":
    raise SystemExit(run_launcher())
