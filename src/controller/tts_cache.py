"""
Local Disk Cache for Synthesized TTS Audio (`TtsDiskCache`)
-----------------------------------------------------------
Stores decoded/compressed TTS audio clips under:
  %LOCALAPPDATA%\\VoiceCompanionData\\tts_cache\\

Design goals:
- Zero external dependencies beyond `numpy` and `soundfile` (already in `.venv`).
- Deterministic SHA-256 keying by `(char_id, voice_slot, backend_tag, normalized_text)`.
- Crash-safe atomic writes via temporary `.tmp` file + `os.replace()` with cleanup.
- True LRU eviction (touches file mtime on cache hit) under a configurable byte cap
  (default 200 MB), plus decompression-bomb & symlink containment guards.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

DEFAULT_CACHE_MAX_BYTES = 200 * 1024 * 1024  # 200 MB
MAX_SINGLE_CACHE_ENTRY_BYTES = 32 * 1024 * 1024  # 32 MB per clip
MAX_DECODED_PCM_SAMPLES = 48_000 * 60 * 15  # 15 min @ 48 kHz safety ceiling
_SUPPORTED_EXTENSIONS: tuple[str, ...] = (".mp3", ".ogg", ".opus", ".flac", ".wav")
_SAFE_KEY_RE = re.compile(r"\A[0-9a-fA-F]{16,128}\Z")
_WHITESPACE_RE = re.compile(r"\s+")


def _default_tts_cache_dir() -> Path:
    """Resolve `%LOCALAPPDATA%\\VoiceCompanionData\\tts_cache` via `app_paths` when available."""
    try:
        from src.utils.app_paths import get_user_data_dir, harden_user_data_acl

        cache_dir = get_user_data_dir() / "tts_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        harden_user_data_acl(cache_dir)
        return cache_dir
    except Exception:
        override = os.environ.get("VOICE_COMPANION_DATA_DIR", "").strip()
        if override:
            base = Path(override).expanduser().resolve()
        else:
            local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
            if local_app_data:
                base = Path(local_app_data) / "VoiceCompanionData"
            else:
                base = Path.home() / "AppData" / "Local" / "VoiceCompanionData"
        cache_dir = base / "tts_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir


def normalize_cache_text(text: str) -> str:
    """Normalize synthesis text for stable cache keying while keeping acting tags intact."""
    s = str(text or "").strip()
    if not s:
        return ""
    return _WHITESPACE_RE.sub(" ", s)


def make_tts_cache_key(
    char_id: str,
    voice_slot: str,
    backend_tag: str,
    text: str,
) -> str:
    """Return a 64-char lowercase SHA-256 hex key for a TTS cache entry."""
    payload = json.dumps(
        [
            str(char_id or "").strip(),
            str(voice_slot or "").strip(),
            str(backend_tag or "").strip(),
            normalize_cache_text(text),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def detect_audio_extension(audio_bytes: bytes, fallback: str = ".mp3") -> str:
    """Sniff audio container magic bytes and return a normalized safe extension."""
    head = bytes(audio_bytes[:16])
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return ".wav"
    if head.startswith(b"fLaC"):
        return ".flac"
    if head.startswith(b"OggS"):
        return ".ogg"
    if head.startswith(b"ID3") or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return ".mp3"
    return _normalize_ext(fallback)


def _normalize_ext(ext: str) -> str:
    raw = str(ext or "").strip().lower()
    if not raw:
        return ".mp3"
    if not raw.startswith("."):
        raw = "." + raw
    if raw not in _SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported cached audio extension: {ext!r}")
    return raw


def decode_audio_bytes_to_pcm(audio_bytes: bytes) -> tuple[np.ndarray, int]:
    """
    Decode MP3 / WAV / OGG / OPUS / FLAC bytes into `(mono_float32, sample_rate)`.
    Raises `ValueError` on empty, corrupt, or absurdly large audio payloads.
    """
    if not isinstance(audio_bytes, (bytes, bytearray, memoryview)) or len(audio_bytes) == 0:
        raise ValueError("Audio payload is empty")
    raw = bytes(audio_bytes)
    if len(raw) > MAX_SINGLE_CACHE_ENTRY_BYTES:
        raise ValueError(f"Audio payload exceeds size limit ({MAX_SINGLE_CACHE_ENTRY_BYTES} bytes)")

    try:
        with sf.SoundFile(io.BytesIO(raw)) as snd:
            sr = int(snd.samplerate)
            channels = int(snd.channels)
            frames = int(snd.frames)
            if sr <= 0 or sr > 384_000:
                raise ValueError(f"Invalid audio sample rate: {sr}")
            if channels <= 0 or channels > 16:
                raise ValueError(f"Invalid audio channel count: {channels}")
            max_frames = max(1, MAX_DECODED_PCM_SAMPLES // channels)
            if frames > max_frames:
                raise ValueError(f"Decoded audio exceeds max sample limit ({frames} * {channels}ch > {MAX_DECODED_PCM_SAMPLES})")
            data = snd.read(frames=max_frames + 1, dtype="float32", always_2d=False)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Failed to decode audio bytes: {exc}") from exc

    arr = np.asarray(data, dtype=np.float32)
    if arr.ndim == 2:
        if arr.shape[0] * arr.shape[1] > MAX_DECODED_PCM_SAMPLES:
            raise ValueError("Decoded audio exceeds max sample limit")
        arr = np.mean(arr, axis=1, dtype=np.float32)
    elif arr.ndim != 1:
        raise ValueError(f"Unexpected decoded PCM dimensions: {arr.ndim}")

    if arr.size == 0:
        raise ValueError("Decoded audio contains zero samples")
    if arr.size > MAX_DECODED_PCM_SAMPLES:
        raise ValueError("Decoded audio exceeds max sample limit")

    if not np.all(np.isfinite(arr)):
        arr = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32, copy=False)
    return np.ascontiguousarray(arr, dtype=np.float32), sr


def _validate_cache_key(key: str) -> str:
    k = str(key or "").strip().lower()
    if not _SAFE_KEY_RE.match(k):
        raise ValueError(f"Invalid TTS cache key (expected 16-128 hex chars): {key!r}")
    return k


def _atomic_write_bytes(target: Path, payload: bytes) -> None:
    """Atomically write bytes via `.tmp` + `os.replace()` with guaranteed temp cleanup."""
    if "\x00" in str(target):
        raise ValueError("Invalid cache target path containing NUL byte")
    if target.is_symlink():
        raise ValueError(f"Refusing to overwrite symlinked cache file: {target.name}")
    target.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.stem[:12]}_", suffix=".tmp", dir=str(target.parent))
    fd_closed = False
    try:
        try:
            os.chmod(tmp_name, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "wb") as fh:
            fd_closed = True
            fh.write(payload)
            fh.flush()
        last_err: Exception | None = None
        for attempt in range(6):
            try:
                os.replace(tmp_name, target)
                last_err = None
                break
            except PermissionError as exc:
                last_err = exc
                time.sleep(0.03 * (attempt + 1))
        if last_err is not None:
            raise last_err
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


class TtsDiskCache:
    """
    Thread-safe local disk cache for TTS audio clips with atomic writes and LRU eviction.

    Public API:
      - `TtsDiskCache.make_key(char_id, voice_slot, backend_tag, text) -> str`
      - `get_pcm(key) -> tuple[np.ndarray, int] | None`
      - `get_audio_bytes(key) -> tuple[bytes, str] | None`
      - `put_audio_bytes(key, audio_bytes, ext="auto") -> Path | None`
      - `put_pcm(key, mono_f32, sr) -> Path | None`
      - `has_key(key) -> bool`
      - `delete(key) -> bool`
      - `clear() -> int`
      - `stats() -> dict[str, Any]`
    """

    def __init__(
        self,
        cache_dir: Path | str | None = None,
        *,
        max_bytes: int = DEFAULT_CACHE_MAX_BYTES,
        enabled: bool = True,
    ) -> None:
        self._lock = threading.RLock()
        self.enabled = bool(enabled)
        try:
            parsed_max = int(max_bytes) if not isinstance(max_bytes, bool) else DEFAULT_CACHE_MAX_BYTES
        except Exception:
            parsed_max = DEFAULT_CACHE_MAX_BYTES
        self.max_bytes = max(1024, parsed_max)
        if cache_dir is None:
            self.cache_dir = _default_tts_cache_dir()
        else:
            raw_p = Path(cache_dir).expanduser()
            if "\x00" in str(raw_p):
                raise ValueError("Invalid cache directory containing NUL byte")
            if raw_p.is_symlink():
                raise ValueError(f"Refusing symlinked cache directory: {raw_p}")
            p = raw_p.resolve()
            p.mkdir(parents=True, exist_ok=True)
            self.cache_dir = p
        self._hits = 0
        self._misses = 0

    @staticmethod
    def make_key(
        char_id: str,
        voice_slot: str,
        backend_tag: str,
        text: str,
    ) -> str:
        """Compute deterministic SHA-256 hex cache key."""
        return make_tts_cache_key(char_id, voice_slot, backend_tag, text)

    def _entry_path(self, safe_key: str, ext: str) -> Path:
        candidate = (self.cache_dir / f"{safe_key}{ext}").resolve()
        if candidate.parent != self.cache_dir.resolve():
            raise ValueError("Cache path containment violation")
        return candidate

    def _find_entry_unlocked(self, safe_key: str) -> Path | None:
        for ext in _SUPPORTED_EXTENSIONS:
            p = self.cache_dir / f"{safe_key}{ext}"
            try:
                if p.is_symlink():
                    continue
                if p.is_file():
                    resolved = p.resolve()
                    if resolved.parent == self.cache_dir.resolve():
                        return resolved
            except OSError:
                continue
        return None

    def _remove_other_extensions_unlocked(self, safe_key: str, keep_ext: str) -> None:
        for ext in _SUPPORTED_EXTENSIONS:
            if ext == keep_ext:
                continue
            p = self.cache_dir / f"{safe_key}{ext}"
            try:
                if p.is_file() and not p.is_symlink():
                    p.unlink()
            except OSError:
                pass

    def _evict_if_needed_unlocked(self, protected_path: Path | None = None) -> int:
        """Evict least-recently-used entries by mtime until total cache size <= `max_bytes`."""
        entries: list[tuple[float, int, Path]] = []
        total_size = 0
        now = time.time()

        try:
            for item in self.cache_dir.iterdir():
                try:
                    if item.is_symlink() or not item.is_file():
                        continue
                    name = item.name
                    if name.endswith(".tmp"):
                        st_tmp = item.stat()
                        if now - st_tmp.st_mtime > 300.0:
                            item.unlink(missing_ok=True)
                        continue
                    if item.suffix.lower() not in _SUPPORTED_EXTENSIONS:
                        continue
                    st = item.stat()
                    total_size += st.st_size
                    entries.append((st.st_mtime_ns, st.st_size, item))
                except OSError:
                    continue
        except OSError:
            return 0

        if total_size <= self.max_bytes:
            return 0

        entries.sort(key=lambda row: row[0])  # oldest mtime first (LRU)
        removed = 0
        protected_resolved = protected_path.resolve() if protected_path is not None else None
        for _mtime, fsize, fpath in entries:
            if total_size <= self.max_bytes:
                break
            try:
                if protected_resolved is not None and fpath.resolve() == protected_resolved:
                    continue
                fpath.unlink()
                total_size = max(0, total_size - fsize)
                removed += 1
            except OSError:
                continue
        return removed

    def has_key(self, key: str) -> bool:
        if not self.enabled:
            return False
        safe_key = _validate_cache_key(key)
        with self._lock:
            return self._find_entry_unlocked(safe_key) is not None

    def get_audio_bytes(self, key: str) -> tuple[bytes, str] | None:
        """Return `(raw_audio_bytes, ext)` on hit and touch mtime for LRU, or `None` on miss."""
        if not self.enabled:
            return None
        safe_key = _validate_cache_key(key)
        with self._lock:
            entry = self._find_entry_unlocked(safe_key)
            if entry is None:
                self._misses += 1
                return None
            try:
                st = entry.stat()
                if st.st_size <= 0 or st.st_size > MAX_SINGLE_CACHE_ENTRY_BYTES:
                    entry.unlink(missing_ok=True)
                    self._misses += 1
                    return None
                with entry.open("rb") as fh:
                    raw = fh.read(MAX_SINGLE_CACHE_ENTRY_BYTES + 1)
                if not raw or len(raw) > MAX_SINGLE_CACHE_ENTRY_BYTES:
                    entry.unlink(missing_ok=True)
                    self._misses += 1
                    return None
                try:
                    os.utime(entry, None)
                except OSError:
                    pass
                self._hits += 1
                return raw, entry.suffix.lower()
            except OSError:
                self._misses += 1
                return None

    def get_pcm(self, key: str) -> tuple[np.ndarray, int] | None:
        """
        Look up cached audio by `key` and return `(mono_float32, sample_rate)` on hit,
        or `None` on miss/corrupt entry (corrupt entries are automatically purged).
        """
        if not self.enabled:
            return None
        safe_key = _validate_cache_key(key)
        with self._lock:
            entry = self._find_entry_unlocked(safe_key)
            if entry is None:
                self._misses += 1
                return None
            try:
                st = entry.stat()
                if st.st_size <= 0 or st.st_size > MAX_SINGLE_CACHE_ENTRY_BYTES:
                    entry.unlink(missing_ok=True)
                    self._misses += 1
                    return None
                with entry.open("rb") as fh:
                    raw = fh.read(MAX_SINGLE_CACHE_ENTRY_BYTES + 1)
                if not raw or len(raw) > MAX_SINGLE_CACHE_ENTRY_BYTES:
                    entry.unlink(missing_ok=True)
                    self._misses += 1
                    return None
            except OSError:
                self._misses += 1
                return None

            try:
                pcm, sr = decode_audio_bytes_to_pcm(raw)
            except ValueError:
                # Self-heal corrupt cache entries
                try:
                    entry.unlink(missing_ok=True)
                except OSError:
                    pass
                self._misses += 1
                return None

            try:
                os.utime(entry, None)
            except OSError:
                pass
            self._hits += 1
            return pcm, sr

    def put_audio_bytes(self, key: str, audio_bytes: bytes, ext: str = "auto") -> Path | None:
        """Atomically store raw audio bytes (`mp3`/`wav`/`ogg`/`opus`/`flac`) and enforce LRU cap."""
        if not self.enabled:
            return None
        safe_key = _validate_cache_key(key)
        if not isinstance(audio_bytes, (bytes, bytearray, memoryview)) or len(audio_bytes) == 0:
            return None
        raw = bytes(audio_bytes)
        if len(raw) > min(self.max_bytes, MAX_SINGLE_CACHE_ENTRY_BYTES):
            return None

        norm_ext = detect_audio_extension(raw) if str(ext or "").strip().lower() in ("", "auto") else _normalize_ext(ext)
        with self._lock:
            target = self._entry_path(safe_key, norm_ext)
            _atomic_write_bytes(target, raw)
            self._remove_other_extensions_unlocked(safe_key, norm_ext)
            self._evict_if_needed_unlocked(protected_path=target)
            return target

    def put_pcm(self, key: str, mono_f32: np.ndarray, sr: int) -> Path | None:
        """Encode mono `float32` PCM as lossless 16-bit WAV/FLAC and store atomically."""
        if not self.enabled:
            return None
        safe_key = _validate_cache_key(key)
        if isinstance(sr, bool):
            raise ValueError(f"Invalid sample rate: {sr!r}")
        sample_rate = int(sr)
        if sample_rate <= 0 or sample_rate > 384_000:
            raise ValueError(f"Invalid sample rate: {sr}")

        arr = np.asarray(mono_f32, dtype=np.float32)
        if arr.ndim == 2:
            arr = np.mean(arr, axis=1, dtype=np.float32)
        elif arr.ndim != 1:
            raise ValueError(f"Expected 1D/2D PCM array, got ndim={arr.ndim}")
        if arr.size == 0 or arr.size > MAX_DECODED_PCM_SAMPLES:
            return None

        arr = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32, copy=False)
        clipped = np.clip(arr, -1.0, 1.0)
        buf = io.BytesIO()
        ext = ".wav"
        try:
            # Prefer FLAC for ~50% smaller lossless disk footprint when libsndfile supports it
            sf.write(buf, clipped, sample_rate, format="FLAC", subtype="PCM_16")
            ext = ".flac"
        except Exception:
            buf = io.BytesIO()
            sf.write(buf, clipped, sample_rate, format="WAV", subtype="PCM_16")
            ext = ".wav"
        return self.put_audio_bytes(safe_key, buf.getvalue(), ext=ext)

    def delete(self, key: str) -> bool:
        safe_key = _validate_cache_key(key)
        removed = False
        with self._lock:
            for ext in _SUPPORTED_EXTENSIONS:
                p = self.cache_dir / f"{safe_key}{ext}"
                try:
                    if p.is_file() and not p.is_symlink():
                        p.unlink()
                        removed = True
                except OSError:
                    pass
        return removed

    def clear(self) -> int:
        """Delete all cached audio files and temporary files in `cache_dir`."""
        removed = 0
        with self._lock:
            if not self.cache_dir.exists():
                return 0
            for item in list(self.cache_dir.iterdir()):
                try:
                    if item.is_symlink() or not item.is_file():
                        continue
                    if item.suffix.lower() in _SUPPORTED_EXTENSIONS or item.name.endswith(".tmp"):
                        item.unlink()
                        removed += 1
                except OSError:
                    continue
        return removed

    def stats(self) -> dict[str, Any]:
        """Return cache directory usage and hit/miss counters."""
        file_count = 0
        total_bytes = 0
        with self._lock:
            if self.cache_dir.exists():
                for item in self.cache_dir.iterdir():
                    try:
                        if item.is_symlink() or not item.is_file():
                            continue
                        if item.suffix.lower() not in _SUPPORTED_EXTENSIONS:
                            continue
                        total_bytes += item.stat().st_size
                        file_count += 1
                    except OSError:
                        continue
            return {
                "cache_dir": str(self.cache_dir),
                "enabled": self.enabled,
                "file_count": file_count,
                "total_bytes": total_bytes,
                "total_mb": round(total_bytes / (1024 * 1024), 2),
                "max_bytes": self.max_bytes,
                "max_mb": round(self.max_bytes / (1024 * 1024), 2),
                "hits": self._hits,
                "misses": self._misses,
            }
