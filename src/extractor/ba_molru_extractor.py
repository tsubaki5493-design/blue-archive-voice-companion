"""
Blue Archive (Steam PC) .molru Voice Extractor & RVC Dataset Builder
--------------------------------------------------------------------
- Parses MediaCatalog.bytes (MXMC) once and caches to data/catalog_cache.json
- Extracts bit-exact .ogg streams from VOC_KR / VOC_JP .molru containers
- Restores original voice filenames (e.g. Aru_MemorialLobby_1.ogg)
- Filters out short grunts / damage screams and exports 40kHz mono WAV + ZIP for RVC v2 training
- Designed to scale from 1 character (Aru) to the entire Blue Archive roster.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import struct
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_CATALOG_MEM_CACHE: Dict[str, Tuple[int, int, Dict[str, List[dict]]]] = {}

DEFAULT_GAME_DIR = Path(r"C:\Program Files (x86)\Steam\steamapps\common\BlueArchive")
_STEAM_VDF_PATH_RE = re.compile(r'"path"\s+"([^"]+)"', re.IGNORECASE)
_DISCOVER_CACHE: Optional[Tuple[float, Optional[Path]]] = None
WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from src.utils.app_paths import get_user_data_dir

DEFAULT_DATA_DIR = get_user_data_dir() / "data"

# Security & resource limits for untrusted/corrupted binary archive parsing
MAX_CATALOG_FILE_BYTES = 128 * 1024 * 1024   # 128 MB hard cap on MediaCatalog.bytes
MAX_CATALOG_ENTRIES = 500_000                # Max catalog entries parsed
MAX_MOLRU_FILE_BYTES = 256 * 1024 * 1024     # 256 MB hard cap on a single .molru pack
MAX_OGG_STREAM_BYTES = 32 * 1024 * 1024      # 32 MB hard cap per extracted .ogg stream
MAX_OGG_STREAMS_PER_PACK = 4096              # Max streams per .molru pack

_SAFE_ENTITY_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_\-]{0,79}\Z")
_WIN_UNSAFE_FILE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_WIN_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
)
_SAFE_EXT_RE = re.compile(r"\A\.[A-Za-z0-9]{1,8}\Z")


def is_safe_entity_id(value: str) -> bool:
    """Return True iff `value` is a strictly safe flat identifier (no traversal, separators, dots, or device names)."""
    s = str(value or "").strip()
    if not s or not _SAFE_ENTITY_ID_RE.match(s):
        return False
    return s.upper() not in _WIN_RESERVED_NAMES


def sanitize_extract_filename(
    raw_name: str,
    *,
    default_ext: str = ".ogg",
    fallback_stem: str = "clip",
) -> str:
    """
    Strictly sanitize an archive/catalog filename against Zip-Slip, path traversal (`..`, `/`, `\\`),
    Windows reserved device names, and control characters.
    """
    text = str(raw_name or "").replace("\x00", "")
    # Take only the final path component across BOTH POSIX and Windows separators
    parts = [p for p in re.split(r"[/\\]+", text) if p and p not in (".", "..")]
    leaf = parts[-1] if parts else ""
    if leaf:
        p_leaf = Path(leaf)
        raw_stem = p_leaf.stem
        raw_ext = p_leaf.suffix
    else:
        raw_stem = ""
        raw_ext = ""

    cleaned = _WIN_UNSAFE_FILE_RE.sub("_", raw_stem)
    cleaned = re.sub(r"\.{2,}", "_", cleaned)
    cleaned = re.sub(r"\s+", "_", cleaned).strip(" .")
    if not cleaned or cleaned.upper() in _WIN_RESERVED_NAMES:
        fb = _WIN_UNSAFE_FILE_RE.sub("_", str(fallback_stem or "clip"))
        fb = re.sub(r"\.{2,}", "_", fb).strip(" .") or "clip"
        cleaned = f"_{cleaned}" if (cleaned and cleaned.upper() in _WIN_RESERVED_NAMES) else fb
    cleaned = cleaned[:96]

    if default_ext:
        ext = default_ext if default_ext.startswith(".") else f".{default_ext}"
    else:
        ext = raw_ext if _SAFE_EXT_RE.match(raw_ext) else ""
    return f"{cleaned}{ext}"


def _ensure_path_within(base_dir: Path, target_path: Path) -> Path:
    """Verify `target_path` resolves strictly inside `base_dir` and is not a symlink."""
    base_res = base_dir.resolve()
    if target_path.is_symlink():
        raise ValueError(f"Refusing to access symlink target: {target_path}")
    target_res = target_path.resolve()
    if not target_res.is_relative_to(base_res):
        raise ValueError(f"Path traversal blocked: {target_path} escapes {base_dir}")
    return target_res


def _assert_not_inside_game_dir(out_dir: Path, game_dir: Path) -> None:
    """Ensure extractor outputs are NEVER written inside the read-only Steam Blue Archive directory."""
    try:
        out_res = out_dir.resolve()
        game_res = game_dir.resolve()
        if out_res == game_res or out_res.is_relative_to(game_res):
            raise ValueError(f"Refusing to write inside read-only Steam game directory: {out_dir}")
    except ValueError:
        raise
    except OSError:
        pass


def _normalize_ba_candidate(candidate: Path | str | None) -> Optional[Path]:
    """Normalize a user-supplied folder (also accepting BlueArchive_Data or BlueArchive.exe)."""
    if candidate is None:
        return None
    raw = str(candidate).strip().strip('"').strip("'")
    if not raw:
        return None
    p = Path(raw)
    if p.is_file() and p.name.lower() == "bluearchive.exe":
        p = p.parent
    if p.name.lower() == "bluearchive_data":
        p = p.parent
    return p


def is_valid_ba_game_dir(candidate: Path | str | None) -> bool:
    """Return True when `candidate` points to a Steam Blue Archive installation root."""
    p = _normalize_ba_candidate(candidate)
    if p is None:
        return False
    res_dir = p / "BlueArchive_Data" / "StreamingAssets" / "PUB" / "Resource"
    if not res_dir.is_dir():
        return False
    audio_dir = res_dir / "GameData" / "MediaResources" / "Audio"
    win_dir = res_dir / "GameData" / "Windows"
    return audio_dir.is_dir() or win_dir.is_dir()


def _parse_steam_library_vdf_paths(vdf_path: Path) -> List[Path]:
    """Extract all Steam library root paths from Valve's `libraryfolders.vdf`."""
    roots: List[Path] = []
    if not vdf_path.is_file():
        return roots
    try:
        text = vdf_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return roots
    seen: set[str] = set()
    for match in _STEAM_VDF_PATH_RE.finditer(text):
        raw_path = match.group(1).replace("\\\\", "\\").strip()
        if not raw_path:
            continue
        norm = raw_path.lower()
        if norm not in seen:
            seen.add(norm)
            roots.append(Path(raw_path))
    return roots


def _query_windows_steam_install_roots() -> List[Path]:
    """Query Windows Registry for installed Steam root directories without failing on non-Windows/restricted systems."""
    if os.name != "nt":
        return []
    roots: List[Path] = []
    seen: set[str] = set()
    try:
        import winreg

        reg_targets = (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
        )
        for hive, subkey, val_name in reg_targets:
            try:
                with winreg.OpenKey(hive, subkey) as k:
                    val, _ = winreg.QueryValueEx(k, val_name)
                    sval = str(val or "").strip()
                    if sval:
                        norm = sval.lower()
                        if norm not in seen:
                            seen.add(norm)
                            roots.append(Path(sval))
            except OSError:
                continue
    except Exception:
        pass
    return roots


def discover_steam_blue_archive_dir(
    preferred_dir: Path | str | None = None,
    *,
    use_cache: bool = True,
) -> Optional[Path]:
    """
    Auto-discover a local Steam Blue Archive installation across ANY Windows drive:
      1. Explicit `preferred_dir` (e.g. saved `config.json['steam_game_dir']`)
      2. Windows Steam Registry + `<SteamPath>/steamapps/libraryfolders.vdf`
      3. Common Steam library paths across drives C:..H: + `DEFAULT_GAME_DIR`
    """
    global _DISCOVER_CACHE
    norm_pref = _normalize_ba_candidate(preferred_dir)
    if norm_pref is not None and is_valid_ba_game_dir(norm_pref):
        return norm_pref

    now = time.monotonic()
    if use_cache and _DISCOVER_CACHE is not None and (now - _DISCOVER_CACHE[0]) < 30.0:
        cached = _DISCOVER_CACHE[1]
        if cached is None or is_valid_ba_game_dir(cached):
            return cached

    candidates: List[Path] = []
    seen: set[str] = set()

    def _add(p: Path) -> None:
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            candidates.append(p)

    # 1. Windows Registry Steam install roots + all Steam library folders from libraryfolders.vdf
    for steam_root in _query_windows_steam_install_roots():
        _add(steam_root / "steamapps" / "common" / "BlueArchive")
        for lib_root in _parse_steam_library_vdf_paths(steam_root / "steamapps" / "libraryfolders.vdf"):
            _add(lib_root / "steamapps" / "common" / "BlueArchive")

    # 2. Common Steam folders across C:..H: drives
    for drv in ("C", "D", "E", "F", "G", "H"):
        _add(Path(f"{drv}:\\Program Files (x86)\\Steam\\steamapps\\common\\BlueArchive"))
        _add(Path(f"{drv}:\\Program Files\\Steam\\steamapps\\common\\BlueArchive"))
        _add(Path(f"{drv}:\\SteamLibrary\\steamapps\\common\\BlueArchive"))
        _add(Path(f"{drv}:\\Steam\\steamapps\\common\\BlueArchive"))
        _add(Path(f"{drv}:\\Games\\SteamLibrary\\steamapps\\common\\BlueArchive"))

    _add(DEFAULT_GAME_DIR)

    found: Optional[Path] = None
    for cand in candidates:
        if is_valid_ba_game_dir(cand):
            found = cand
            break

    if use_cache:
        _DISCOVER_CACHE = (now, found)
    return found


def resolve_ba_game_dir(
    game_dir: Path | str | None = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> Path:
    """
    Resolve the effective Blue Archive Steam directory:
      - If caller explicitly passes a custom `game_dir` (different from `DEFAULT_GAME_DIR`), honor it verbatim.
      - Otherwise prefer `cfg['steam_game_dir']` (if valid), then auto-discover via Steam Registry & VDF,
        and fall back to `DEFAULT_GAME_DIR`.
    """
    norm_arg = _normalize_ba_candidate(game_dir)
    if norm_arg is not None and norm_arg != DEFAULT_GAME_DIR:
        return norm_arg

    cfg_hint = str((cfg or {}).get("steam_game_dir") or "").strip() if isinstance(cfg, dict) else ""
    if cfg_hint:
        norm_cfg = _normalize_ba_candidate(cfg_hint)
        if norm_cfg is not None and is_valid_ba_game_dir(norm_cfg):
            return norm_cfg

    discovered = discover_steam_blue_archive_dir(preferred_dir=cfg_hint or None)
    if discovered is not None:
        return discovered
    return norm_arg or DEFAULT_GAME_DIR

FOLDER_ID_PREFIX = {
    0x03: "BlueArchive_Data/StreamingAssets/PUB/Resource/GameData/MediaResources/",
    0x02: "BlueArchive_Data/StreamingAssets/PUB/Resource/Preload/MediaResources/",
    0x01: "BlueArchive_Data/StreamingAssets/",
}

# Keywords indicating non-conversational combat grunts/screams that degrade RVC timbre quality
NOISY_COMBAT_PATTERNS = re.compile(
    r"(Battle_Damage|Battle_Shout|Battle_Retire|Battle_Defense|Battle_Move|Battle_Cover|"
    r"Damage_\d|Shout_\d|ExSkill_Level|_Groggy|_Panic)",
    re.IGNORECASE,
)


@dataclass
class CatalogEntry:
    internal_path: str
    external_path: str
    external_file: str


def parse_media_catalog(catalog_bytes_path: Path) -> Dict[str, List[CatalogEntry]]:
    """Parse Steam Blue Archive MediaCatalog.bytes (MXMC format) into molru_key -> ordered file list."""
    file_size = catalog_bytes_path.stat().st_size
    if file_size > MAX_CATALOG_FILE_BYTES:
        raise ValueError(f"MediaCatalog.bytes exceeds maximum allowed size ({file_size} > {MAX_CATALOG_FILE_BYTES})")
    raw = catalog_bytes_path.read_bytes()
    if len(raw) <= 9:
        raise ValueError(f"Invalid or empty MediaCatalog.bytes: {catalog_bytes_path}")

    pos = 9
    total_len = len(raw)
    catalog: Dict[str, List[CatalogEntry]] = {}
    parsed_count = 0

    while pos + 4 <= total_len and parsed_count < MAX_CATALOG_ENTRIES:
        (internal_len,) = struct.unpack_from("<I", raw, pos)
        pos += 4
        if internal_len > 4096 or pos + internal_len + 5 + 4 > total_len:
            break

        internal_bytes = raw[pos : pos + internal_len]
        pos += internal_len + 5  # 1-byte marker + 4-byte padding

        (external_len,) = struct.unpack_from("<I", raw, pos)
        pos += 4
        if external_len > 4096 or pos + external_len + 1 > total_len:
            break

        external_bytes = raw[pos : pos + external_len]
        pos += external_len

        folder_id = raw[pos]
        pos += 1 + 11  # 1-byte folder_id + 7-byte padding + 4-byte next header field

        prefix = FOLDER_ID_PREFIX.get(
            folder_id,
            "BlueArchive_Data/StreamingAssets/PUB/Resource/GameData/MediaResources/",
        )
        internal_str = internal_bytes.decode("utf-8", errors="replace")
        external_str = external_bytes.decode("utf-8", errors="replace").replace("\\", "/")

        parts = [p for p in external_str.split("/") if p not in ("", ".", "..")]
        dir_part = "/".join(parts[:-1]) if len(parts) > 1 else ""
        key = f"{prefix}{dir_part}"
        raw_external_file = parts[-1] if parts else external_str
        external_file = sanitize_extract_filename(raw_external_file, default_ext="", fallback_stem="clip")

        catalog.setdefault(key, []).append(
            CatalogEntry(
                internal_path=internal_str,
                external_path=external_str,
                external_file=external_file,
            )
        )
        parsed_count += 1

    return catalog


def load_or_build_catalog(game_dir: Path, cache_path: Path, force_refresh: bool = False) -> Dict[str, List[dict]]:
    """
    Load cached catalog JSON (with in-memory cache across 305-character batch runs) or parse MediaCatalog.bytes.
    Automatically rebuilds if Steam updated MediaCatalog.bytes or if the JSON cache was corrupted.
    """
    catalog_bytes = (
        game_dir
        / "BlueArchive_Data"
        / "StreamingAssets"
        / "PUB"
        / "Resource"
        / "Catalog"
        / "MediaResources"
        / "MediaCatalog.bytes"
    )
    _assert_not_inside_game_dir(cache_path, game_dir)
    if cache_path.is_symlink():
        raise ValueError(f"Refusing to access symlink catalog cache: {cache_path}")
    resolved_cache = str(cache_path.resolve())
    cache_stale = False
    if cache_path.exists() and catalog_bytes.exists():
        try:
            cache_stale = catalog_bytes.stat().st_mtime > (cache_path.stat().st_mtime + 2.0)
        except OSError:
            cache_stale = False

    if cache_path.exists() and not force_refresh and not cache_stale:
        try:
            st = cache_path.stat()
            if st.st_size <= MAX_CATALOG_FILE_BYTES:
                mem = _CATALOG_MEM_CACHE.get(resolved_cache)
                if mem and mem[0] == st.st_mtime_ns and mem[1] == st.st_size:
                    return mem[2]
                data = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data:
                    _CATALOG_MEM_CACHE[resolved_cache] = (st.st_mtime_ns, st.st_size, data)
                    return data
        except Exception:
            # Corrupted/truncated cache file -> fall through and rebuild cleanly from MediaCatalog.bytes
            pass

    if not catalog_bytes.exists():
        print(f"[WARN] MediaCatalog.bytes not found at {catalog_bytes}; proceeding without filename restoration.")
        return {}

    parsed = parse_media_catalog(catalog_bytes)
    serializable = {k: [asdict(e) for e in v] for k, v in parsed.items()}
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(serializable, ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(prefix=".cat_", suffix=".tmp", dir=str(cache_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        for attempt in range(5):
            try:
                os.replace(tmp_name, cache_path)
                break
            except PermissionError:
                time.sleep(0.04 * (attempt + 1))
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
    try:
        st = cache_path.stat()
        _CATALOG_MEM_CACHE[resolved_cache] = (st.st_mtime_ns, st.st_size, serializable)
    except OSError:
        pass
    print(f"[OK] Cached MediaCatalog ({sum(len(v) for v in serializable.values())} entries) -> {cache_path}")
    return serializable


def compute_exact_ogg_stream_end(
    data: bytes,
    start_offset: int,
    max_end: int,
    *,
    max_stream_bytes: int = MAX_OGG_STREAM_BYTES,
) -> int:
    """
    Walk RFC 3533 OGG pages from start_offset for the same bitstream serial number
    so trailing .molru gap metadata bytes are cleanly excluded, bounded by `max_stream_bytes`.
    """
    capped_max_end = min(max_end, start_offset + max(1, int(max_stream_bytes)))
    if start_offset + 27 > capped_max_end or data[start_offset : start_offset + 4] != b"OggS":
        return capped_max_end

    target_serial = data[start_offset + 14 : start_offset + 18]
    pos = start_offset
    last_valid_end = start_offset

    while pos + 27 <= capped_max_end and data[pos : pos + 4] == b"OggS":
        version = data[pos + 4]
        header_type = data[pos + 5]
        serial = data[pos + 14 : pos + 18]
        if version != 0 or serial != target_serial:
            break

        num_segments = data[pos + 26]
        seg_table_end = pos + 27 + num_segments
        if seg_table_end > capped_max_end:
            break

        body_size = sum(data[pos + 27 : seg_table_end])
        page_end = seg_table_end + body_size
        if page_end > capped_max_end:
            break

        last_valid_end = page_end
        # 0x04 = EOS (End Of Stream) page flag
        if header_type & 0x04:
            break
        pos = page_end

    return last_valid_end if last_valid_end > start_offset else capped_max_end


def extract_ogg_streams_from_molru(
    molru_bytes: bytes,
    *,
    max_stream_bytes: int = MAX_OGG_STREAM_BYTES,
    max_streams: int = MAX_OGG_STREAMS_PER_PACK,
) -> List[Tuple[int, int, bytes]]:
    """Scan a .molru archive and extract all distinct OGG audio streams in order with strict size caps."""
    total_len = len(molru_bytes)
    if total_len > MAX_MOLRU_FILE_BYTES:
        raise ValueError(f".molru pack exceeds maximum allowed size ({total_len} > {MAX_MOLRU_FILE_BYTES})")
    eff_max_stream = max(1, min(int(max_stream_bytes), MAX_OGG_STREAM_BYTES))
    eff_max_streams = max(1, min(int(max_streams), MAX_OGG_STREAMS_PER_PACK))

    streams_raw_starts: List[int] = []
    current_serial: Optional[bytes] = None

    search_pos = 0
    while len(streams_raw_starts) < eff_max_streams:
        idx = molru_bytes.find(b"OggS", search_pos)
        if idx == -1 or idx + 27 > total_len:
            break

        # Verify OGG capture pattern version == 0
        if molru_bytes[idx + 4] == 0:
            serial = molru_bytes[idx + 14 : idx + 18]
            if serial != current_serial:
                streams_raw_starts.append(idx)
                current_serial = serial
            # Jump past this page header to speed up scanning
            n_seg = molru_bytes[idx + 26]
            seg_end = idx + 27 + n_seg
            if seg_end <= total_len:
                page_len = 27 + n_seg + sum(molru_bytes[idx + 27 : seg_end])
                search_pos = max(idx + 4, idx + page_len)
                continue

        search_pos = idx + 4

    extracted: List[Tuple[int, int, bytes]] = []
    for i, start in enumerate(streams_raw_starts):
        next_boundary = streams_raw_starts[i + 1] if i + 1 < len(streams_raw_starts) else total_len
        exact_end = compute_exact_ogg_stream_end(
            molru_bytes, start, next_boundary, max_stream_bytes=eff_max_stream
        )
        stream_len = exact_end - start
        if stream_len <= 0 or stream_len > eff_max_stream:
            continue
        extracted.append((start, exact_end, molru_bytes[start:exact_end]))

    return extracted


# Known Blue Archive .molru romanization/spelling aliases (character_id lowercase -> extra prefixes)
CHARACTER_MOLRU_ALIASES: Dict[str, Tuple[str, ...]] = {
    "reijo": ("reizyo",),
    "aris": ("alice",),
}


def find_character_molru_files(
    game_dir: Path,
    character_prefix: str,
    lang: str,
    explicit_packs: Optional[List[str]] = None,
    extra_prefixes: Optional[List[str]] = None,
) -> List[Path]:
    """
    Find all .molru voice packs matching a character (including costume variants like Newyear, Dress, Swimsuit).
    Example: character_prefix='Aru', lang='KR' -> matches KR_Aru.molru, KR_AruNewyear.molru, KR_AruDress.molru, etc.
    Does NOT accidentally match unrelated names (e.g. 'Aris' vs 'Aru').
    Also supports explicit pack stems from config/characters/<id>.json (e.g. ['JP_CH0069', 'JP_NP0035'])
    and case-insensitive matching (e.g. JP_erika.molru) plus romanization aliases (e.g. Reijo -> reizyo).
    """
    lang_up = str(lang or "JP").strip().upper()
    if lang_up not in ("JP", "KR"):
        return []
    voc_dir = (
        game_dir
        / "BlueArchive_Data"
        / "StreamingAssets"
        / "PUB"
        / "Resource"
        / "GameData"
        / "MediaResources"
        / "Audio"
        / f"VOC_{lang_up}"
    )
    if not voc_dir.exists():
        return []

    voc_dir_res = voc_dir.resolve()
    all_molrus = sorted(voc_dir.glob("*.molru"))
    stem_to_path_ci = {p.stem.lower(): p for p in all_molrus}

    found: List[Path] = []
    seen = set()
    if explicit_packs:
        for pk in explicit_packs:
            raw_pk = str(pk or "").strip()
            if not raw_pk or "/" in raw_pk or "\\" in raw_pk or ".." in raw_pk or "\x00" in raw_pk:
                continue
            stem = raw_pk if raw_pk.upper().startswith(f"{lang_up}_") else f"{lang_up}_{raw_pk}"
            if not _SAFE_ENTITY_ID_RE.match(stem):
                continue
            cand = voc_dir / f"{stem}.molru"
            if not cand.exists():
                cand = stem_to_path_ci.get(stem.lower(), cand)
            try:
                if cand.exists() and not cand.is_symlink() and cand.resolve().is_relative_to(voc_dir_res) and cand not in seen:
                    seen.add(cand)
                    found.append(cand)
            except OSError:
                continue

    # Collect deduplicated candidate prefixes (primary molru_prefix + extra_prefixes + romanization aliases)
    prefixes: List[str] = []
    for raw_p in [character_prefix, *(extra_prefixes or [])]:
        sp = str(raw_p or "").strip()
        if sp and sp.lower() not in {x.lower() for x in prefixes}:
            prefixes.append(sp)
        for alias in CHARACTER_MOLRU_ALIASES.get(sp.lower(), ()):
            if alias.lower() not in {x.lower() for x in prefixes}:
                prefixes.append(alias)

    # Prefix collision guard pairs: (prefix_lower, forbidden_bare_startswith)
    collision_guards: Dict[str, Tuple[str, ...]] = {
        "aru": ("aris", "arata"),
        "mari": ("marina",),
        "hina": ("hinata",),
        "shun": ("shuns",),
        "rei": ("reizyo", "reijo", "reisa"),
        "eri": ("erika", "erina"),
        "hana": ("hanae", "hanako"),
        "haru": ("haruka", "haruna"),
        "mio": ("miori",),
        "miyu": ("miyuki",),
        "natsu": ("natsuki",),
        "sena": ("senna",),
        "yuzu": ("yuzuki",),
        "mika": ("mikamo",),
        "noa": ("noah",),
        "ren": ("renge",),
        "saki": ("sakiko",),
        "umi": ("umika",),
        "aoi": ("aoba",),
        "kanna": ("kannagi",),
    }

    for pref in prefixes:
        pref_low = pref.lower()
        # Match `<LANG>_<pref>` case-insensitively when followed by end-of-stem, underscore, digit, or uppercase costume suffix
        # (using original case on the boundary char so `JP_Aru` matches `JP_AruDress` but NOT `JP_Aris`, while still matching `JP_erika`)
        for p in all_molrus:
            if p in seen:
                continue
            stem = p.stem
            if not stem.upper().startswith(f"{lang_up}_"):
                continue
            bare = stem[len(lang_up) + 1 :]
            bare_low = bare.lower()
            if not bare_low.startswith(pref_low):
                continue
            rest = bare[len(pref) :]
            if rest and not (rest[0] == "_" or rest[0].isdigit() or rest[0].isupper()):
                continue
            if any(bare_low.startswith(bad) for bad in collision_guards.get(pref_low, ())):
                continue
            seen.add(p)
            found.append(p)
    return found


def list_all_voiced_characters(catalog: Dict[str, List[dict]]) -> Dict[str, List[str]]:
    """Extract list of all voiced character packs available in VOC_KR and VOC_JP from cached catalog."""
    result = {"KR": [], "JP": []}
    for key in sorted(catalog.keys()):
        if "/Audio/VOC_KR/" in key:
            result["KR"].append(key.split("/")[-1])
        elif "/Audio/VOC_JP/" in key:
            result["JP"].append(key.split("/")[-1])
    return result


def build_clean_rvc_wav_dataset(
    raw_ogg_files: List[Path],
    wav_out_dir: Path,
    zip_out_path: Path,
    target_sr: int = 40000,
    min_duration_sec: float = 1.1,
    max_duration_sec: float = 15.0,
) -> dict:
    """
    Convert clean conversational .ogg voice lines to 40kHz 16-bit mono WAV files
    (the native sample rate of RVC v2 40k) and bundle into a ready-to-train .zip.
    """
    import numpy as np
    import soundfile as sf
    from math import gcd
    from scipy.signal import resample_poly

    wav_out_dir.mkdir(parents=True, exist_ok=True)
    kept_files: List[Path] = []
    skipped_combat = 0
    skipped_short = 0
    total_duration_sec = 0.0

    for ogg_path in raw_ogg_files:
        if NOISY_COMBAT_PATTERNS.search(ogg_path.stem):
            skipped_combat += 1
            continue

        try:
            audio, sr = sf.read(str(ogg_path), dtype="float32", always_2d=False)
        except Exception as exc:
            print(f"  [WARN] Could not decode {ogg_path.name}: {exc}")
            continue

        if audio.ndim > 1:
            audio = np.mean(audio, axis=1)

        # Trim leading/trailing digital silence (-45 dBFS threshold)
        non_silent = np.where(np.abs(audio) > 0.0056)[0]
        if len(non_silent) == 0:
            skipped_short += 1
            continue

        pad = int(sr * 0.08)
        s_idx = max(0, int(non_silent[0]) - pad)
        e_idx = min(len(audio), int(non_silent[-1]) + pad)
        audio = audio[s_idx:e_idx]

        duration = len(audio) / float(sr)
        if duration < min_duration_sec or duration > max_duration_sec:
            skipped_short += 1
            continue

        # Resample to target_sr (40000 Hz for RVC v2)
        if sr != target_sr:
            g = gcd(sr, target_sr)
            audio = resample_poly(audio, target_sr // g, sr // g).astype(np.float32)

        # Peak normalize gently to -1.5 dBFS (0.84) to avoid clipping and ensure consistent RVC volume
        peak = float(np.max(np.abs(audio)))
        if peak > 1e-4:
            audio = audio * (0.84 / peak)

        safe_wav_name = sanitize_extract_filename(ogg_path.stem, default_ext=".wav", fallback_stem="clip")
        wav_path = _ensure_path_within(wav_out_dir, wav_out_dir / safe_wav_name)
        sf.write(str(wav_path), audio, target_sr, subtype="PCM_16")
        kept_files.append(wav_path)
        total_duration_sec += duration

    if zip_out_path.is_symlink():
        raise ValueError(f"Refusing to overwrite symlink ZIP target: {zip_out_path}")
    # Create RVC dataset ZIP archive
    with zipfile.ZipFile(zip_out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for wav_file in kept_files:
            arc_name = sanitize_extract_filename(wav_file.name, default_ext=".wav", fallback_stem="clip")
            zf.write(wav_file, arcname=arc_name)

    return {
        "total_raw_ogg": len(raw_ogg_files),
        "clean_wav_count": len(kept_files),
        "skipped_combat_grunts": skipped_combat,
        "skipped_too_short_or_long": skipped_short,
        "clean_total_duration_sec": round(total_duration_sec, 1),
        "clean_total_duration_min": round(total_duration_sec / 60.0, 2),
        "zip_path": str(zip_out_path),
    }


def extract_character(
    game_dir: Path,
    data_dir: Path,
    character: str,
    langs: List[str],
    build_wav: bool = True,
) -> dict:
    if not is_safe_entity_id(character):
        raise ValueError(f"Invalid or unsafe character identifier: {character!r}")
    _assert_not_inside_game_dir(data_dir, game_dir)
    catalog_cache_path = data_dir / "catalog_cache.json"
    catalog = load_or_build_catalog(game_dir, catalog_cache_path)

    # Auto-resolve molru_prefix and explicit local_voice_packs if config/characters/<id>.json exists
    chars_cfg_dir = WORKSPACE_ROOT / "config" / "characters"
    char_cfg_path = _ensure_path_within(chars_cfg_dir, chars_cfg_dir / f"{character.lower()}.json")
    molru_prefix = character
    local_packs_cfg: dict = {}
    if char_cfg_path.exists():
        try:
            c_json = json.loads(char_cfg_path.read_text(encoding="utf-8"))
            molru_prefix = c_json.get("molru_prefix") or character
            local_packs_cfg = c_json.get("local_voice_packs") or {}
        except Exception:
            pass

    summary = {"character": character, "languages": {}}
    game_dir_res = game_dir.resolve()

    for lang in langs:
        lang_upper = str(lang or "").strip().upper()
        if lang_upper not in ("JP", "KR"):
            raise ValueError(f"Invalid language code: {lang!r}")
        explicit_list = local_packs_cfg.get(lang_upper.lower()) or []
        molru_files = find_character_molru_files(game_dir, molru_prefix, lang_upper, explicit_packs=explicit_list)
        if not molru_files:
            print(f"[WARN] No .molru files found for character='{character}' (prefix='{molru_prefix}') lang='{lang_upper}'")
            continue

        char_lang_dir = _ensure_path_within(data_dir, data_dir / "characters" / character / lang_upper)
        raw_ogg_dir = _ensure_path_within(char_lang_dir, char_lang_dir / "raw_ogg")
        wav_40k_dir = _ensure_path_within(char_lang_dir, char_lang_dir / "clean_wav_40k")
        zip_path = _ensure_path_within(char_lang_dir, char_lang_dir / f"{character}_{lang_upper}_rvc_dataset.zip")
        raw_ogg_dir.mkdir(parents=True, exist_ok=True)

        extracted_ogg_paths: List[Path] = []
        pack_details = []

        for molru_path in molru_files:
            if molru_path.is_symlink() or not molru_path.resolve().is_relative_to(game_dir_res):
                continue
            if molru_path.stat().st_size > MAX_MOLRU_FILE_BYTES:
                print(f"[WARN] Skipping oversized .molru pack: {molru_path.name}")
                continue
            rel_from_game = molru_path.relative_to(game_dir).as_posix()
            mxmc_key = rel_from_game[:-6] if rel_from_game.endswith(".molru") else rel_from_game
            name_queue = list(catalog.get(mxmc_key, []))

            molru_bytes = molru_path.read_bytes()
            streams = extract_ogg_streams_from_molru(molru_bytes)

            for idx, (start, end, ogg_data) in enumerate(streams):
                fallback_stem = f"{molru_path.stem}_{idx:04d}"
                if idx < len(name_queue):
                    raw_fname = str((name_queue[idx] or {}).get("external_file") or "")
                else:
                    raw_fname = f"{fallback_stem}.ogg"
                fname = sanitize_extract_filename(raw_fname, default_ext=".ogg", fallback_stem=fallback_stem)

                # Prefix costume pack stem if needed to avoid collisions across variants
                stem_suffix = molru_path.stem.split("_", 1)[-1].lower()
                raw_out_name = fname if fname.lower().startswith(stem_suffix) else f"{molru_path.stem}_{fname}"
                out_name = sanitize_extract_filename(raw_out_name, default_ext=".ogg", fallback_stem=fallback_stem)
                out_path = _ensure_path_within(raw_ogg_dir, raw_ogg_dir / out_name)
                out_path.write_bytes(ogg_data)
                extracted_ogg_paths.append(out_path)

            pack_details.append(
                {
                    "pack": molru_path.name,
                    "extracted_count": len(streams),
                    "catalog_matched": len(name_queue),
                }
            )
            print(
                f"[OK] Unpacked {molru_path.name}: {len(streams)} OGG clips "
                f"(Catalog names matched: {min(len(streams), len(name_queue))}/{len(streams)})"
            )

        dataset_stats = {}
        if build_wav and extracted_ogg_paths:
            dataset_stats = build_clean_rvc_wav_dataset(
                raw_ogg_files=extracted_ogg_paths,
                wav_out_dir=wav_40k_dir,
                zip_out_path=zip_path,
            )
            print(
                f"[OK] Built RVC clean dataset for {character} ({lang_upper}): "
                f"{dataset_stats['clean_wav_count']} clips "
                f"({dataset_stats['clean_total_duration_min']} mins) -> {zip_path.name}"
            )

        manifest = {
            "character": character,
            "language": lang_upper,
            "packs": pack_details,
            "rvc_dataset": dataset_stats,
        }
        (char_lang_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        summary["languages"][lang_upper] = manifest

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Blue Archive Steam .molru Voice Extractor & RVC Dataset Builder")
    parser.add_argument("--game-dir", type=Path, default=DEFAULT_GAME_DIR, help="Path to BlueArchive Steam root")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR, help="Workspace data output directory")
    parser.add_argument("--character", type=str, default="Aru", help="Character English ID prefix (e.g., Aru, Hina, Mika)")
    parser.add_argument("--lang", type=str, default="both", choices=["KR", "JP", "both"], help="Voice language to extract")
    parser.add_argument("--list-characters", action="store_true", help="List all voiced character packs from catalog")
    parser.add_argument("--no-wav", action="store_true", help="Extract raw .ogg only without 40kHz WAV conversion")
    args = parser.parse_args()

    if args.list_characters:
        catalog = load_or_build_catalog(args.game_dir, args.data_dir / "catalog_cache.json")
        roster = list_all_voiced_characters(catalog)
        print(f"KR Voiced Packs ({len(roster['KR'])}): {', '.join(roster['KR'][:30])} ...")
        print(f"JP Voiced Packs ({len(roster['JP'])}): {', '.join(roster['JP'][:30])} ...")
        return

    langs = ["KR", "JP"] if args.lang == "both" else [args.lang]
    summary = extract_character(
        game_dir=args.game_dir,
        data_dir=args.data_dir,
        character=args.character,
        langs=langs,
        build_wav=not args.no_wav,
    )
    print("\n=== EXTRACTION SUMMARY ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
