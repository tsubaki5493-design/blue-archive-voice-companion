"""
Subprocess-Isolated Launcher for Optional Local Gemini Live x RVC Runtime
-------------------------------------------------------------------------
Allows the main MomoTalk hub (`aru_fish_bridge.py`) to detect and launch
`src.rvc_live.rvc_live_app` inside its isolated `.venv_rvc` environment
without importing PyTorch/CUDA/RVC modules in the main process, and without
breaking lightweight public distribution builds where `src/rvc_live/` or
`.venv_rvc` is not present.
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
from pathlib import Path
from typing import Any

_SAFE_CHAR_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_\-]{0,79}\Z")
_SANITIZE_ENV_EXACT = frozenset(
    {
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
        "PYTHONINSPECT",
    }
)


def _normalize_project_root(project_root: Path | str | None) -> Path | None:
    if project_root is None:
        return None
    try:
        raw = Path(project_root)
        if "\x00" in str(raw):
            return None
        resolved = raw.resolve()
        return resolved if resolved.is_dir() else None
    except OSError:
        return None


def resolve_rvc_app_script(project_root: Path | str | None) -> Path | None:
    """Return resolved `src/rvc_live/rvc_live_app.py` path if present and strictly inside `project_root`."""
    root = _normalize_project_root(project_root)
    if root is None:
        return None
    try:
        candidate = root / "src" / "rvc_live" / "rvc_live_app.py"
        if not candidate.is_file() or candidate.is_symlink():
            return None
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root):
            return None
        return resolved
    except OSError:
        return None


def resolve_rvc_python(project_root: Path | str | None) -> Path | None:
    """Return resolved `.venv_rvc` Python executable (`pythonw.exe` preferred) if present and verified."""
    root = _normalize_project_root(project_root)
    if root is None:
        return None
    try:
        venv_dir = root / ".venv_rvc"
        if not venv_dir.is_dir() or venv_dir.is_symlink():
            return None
        venv_res = venv_dir.resolve()
        if not venv_res.is_relative_to(root):
            return None
        candidates = (
            (venv_dir / "Scripts" / "pythonw.exe", venv_dir / "Scripts" / "python.exe")
            if os.name == "nt"
            else (venv_dir / "bin" / "python3", venv_dir / "bin" / "python")
        )
        for cand in candidates:
            if not cand.is_file() or cand.is_symlink():
                continue
            cand_res = cand.resolve()
            if cand_res.is_relative_to(venv_res):
                return cand_res
    except OSError:
        return None
    return None


def is_rvc_runtime_available(project_root: Path | str | None) -> bool:
    """Return True iff both `src/rvc_live/rvc_live_app.py` and `.venv_rvc` Python are available."""
    return (resolve_rvc_app_script(project_root) is not None) and (
        resolve_rvc_python(project_root) is not None
    )


def _count_installed_rvc_models(root: Path) -> int:
    roots: list[Path] = [root / "models_rvc"]
    try:
        from src.utils.app_paths import get_user_data_dir

        roots.append(get_user_data_dir() / "models_rvc")
    except Exception:
        pass

    seen_roots: set[str] = set()
    found_pths: set[str] = set()
    for base in roots:
        try:
            if not base.is_dir() or base.is_symlink():
                continue
            key = os.path.normcase(str(base.resolve()))
            if key in seen_roots:
                continue
            seen_roots.add(key)
            for pth in base.rglob("*.pth"):
                if pth.is_file() and not pth.is_symlink() and not pth.name.startswith(("G_", "D_")):
                    found_pths.add(os.path.normcase(str(pth.resolve())))
        except OSError:
            continue
    return len(found_pths)


def get_rvc_status(project_root: Path | str | None) -> dict[str, Any]:
    """Return structured availability and model inventory info for the optional local RVC runtime."""
    root = _normalize_project_root(project_root)
    if root is None:
        return {
            "available": False,
            "app_present": False,
            "venv_present": False,
            "python_exe": "",
            "app_script": "",
            "model_count": 0,
            "reason": "프로젝트 루트 폴더를 확인할 수 없습니다.",
        }

    app_script = resolve_rvc_app_script(root)
    py_exe = resolve_rvc_python(root)
    app_present = app_script is not None
    venv_present = py_exe is not None
    available = app_present and venv_present
    model_count = _count_installed_rvc_models(root) if app_present else 0

    if available:
        reason = (
            f"로컬 Gemini Live × RVC 준비 완료 (설치된 RVC 모델 {model_count}개)"
            if model_count > 0
            else "로컬 Gemini Live × RVC 런타임 준비 완료 (models_rvc 폴더에 .pth 추가 필요)"
        )
    elif not app_present:
        reason = "현재 설치본에는 로컬 RVC 모듈(src/rvc_live)이 포함되어 있지 않습니다."
    else:
        reason = "로컬 RVC 전용 가상환경(.venv_rvc)이 설치되어 있지 않습니다."

    return {
        "available": available,
        "app_present": app_present,
        "venv_present": venv_present,
        "python_exe": str(py_exe) if py_exe is not None else "",
        "app_script": str(app_script) if app_script is not None else "",
        "model_count": model_count,
        "reason": reason,
    }


def _sanitize_char_id(char_id: str | None) -> str | None:
    if char_id is None:
        return None
    s = str(char_id).strip()
    if not s or not _SAFE_CHAR_ID_RE.fullmatch(s):
        return None
    return s


def _normalize_rvc_lang(lang: str | None) -> str | None:
    if lang is None:
        return None
    raw = str(lang).strip().lower()
    if not raw:
        return None
    if raw in ("kr", "ko", "kor", "korean"):
        return "KR"
    if raw in ("jp", "ja", "jpn", "japanese"):
        return "JP"
    return None


def build_sanitized_rvc_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Return a clean child-process environment stripped of Python/pip hijack variables."""
    src_env = dict(os.environ if base_env is None else base_env)
    cleaned: dict[str, str] = {}
    for k, v in src_env.items():
        ku = k.upper()
        if ku in _SANITIZE_ENV_EXACT or ku.startswith("PIP_"):
            continue
        cleaned[k] = v
    cleaned["PYTHONNOUSERSITE"] = "1"
    cleaned["PYTHONUTF8"] = "1"
    cleaned["PYTHONIOENCODING"] = "utf-8"
    return cleaned


def focus_existing_rvc_window() -> bool:
    """Bring an already-open Gemini Live x RVC window to the foreground on Windows."""
    if os.name != "nt":
        return False
    found_ref = [False]
    try:
        user32 = ctypes.WinDLL("user32")
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        user32.EnumWindows.argtypes = [WNDENUMPROC, ctypes.c_void_p]
        user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
        user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]

        def _enum_cb(hwnd: int, _lparam: int) -> int:
            if user32.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd, buf, 256)
                title = buf.value
                if "Gemini Live" in title and "RVC" in title:
                    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                    user32.SetForegroundWindow(hwnd)
                    found_ref[0] = True
                    return 0
            return 1

        user32.EnumWindows(WNDENUMPROC(_enum_cb), 0)
    except Exception:
        pass
    return found_ref[0]


def launch_rvc_live_app(
    project_root: Path | str,
    char_id: str | None = None,
    lang: str | None = None,
) -> subprocess.Popen[Any]:
    """
    Launch `src.rvc_live.rvc_live_app` in an isolated `.venv_rvc` subprocess with
    sanitized environment variables, no console window, and optional `--char` / `--lang`.
    """
    root = _normalize_project_root(project_root)
    if root is None:
        raise RuntimeError("유효한 프로젝트 루트 경로가 아닙니다.")

    app_script = resolve_rvc_app_script(root)
    if app_script is None:
        raise RuntimeError("로컬 RVC 모듈(src/rvc_live/rvc_live_app.py)을 찾을 수 없습니다.")

    py_exe = resolve_rvc_python(root)
    if py_exe is None:
        raise RuntimeError("로컬 RVC 전용 가상환경(.venv_rvc)을 찾을 수 없습니다.")

    argv: list[str] = [str(py_exe), "-m", "src.rvc_live.rvc_live_app"]
    clean_cid = _sanitize_char_id(char_id)
    if clean_cid:
        argv.extend(["--char", clean_cid])
    clean_lang = _normalize_rvc_lang(lang)
    if clean_lang:
        argv.extend(["--lang", clean_lang])

    popen_kwargs: dict[str, Any] = {
        "cwd": str(root),
        "env": build_sanitized_rvc_env(),
        "shell": False,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    return subprocess.Popen(argv, **popen_kwargs)
