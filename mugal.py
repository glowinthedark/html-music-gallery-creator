#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mutagen>=1.47", "enzyme>=0.5"]
# ///
"""
mugal — scan a local music library and generate a browser player.

Writes audiodata.js (track metadata) and index.html (the player) into the
output folder. Open index.html straight from disk (file://); no server needed.

Usage:
    python mugal.py [ROOT] [options]
    uv run mugal.py [ROOT] [options]     # installs mutagen + enzyme automatically

ROOT defaults to the current working directory.

Examples:
    python mugal.py ~/Music
    python mugal.py ~/Music --exclude Podcasts --exclude '*.wav'
    python mugal.py /Volumes/NAS/Music -o ~/player --min-duration 30
    python mugal.py ~/Music --serve            # debug over http://localhost:8000
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import errno
import fnmatch
import functools
import gc
import hashlib
import importlib
import importlib.util
import itertools
import json
import math
import os
import re
import site
import stat
import sys
import sysconfig
import tempfile
import time
import unicodedata
import webbrowser
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

try:
    import mutagen
    from mutagen import File as MutagenFile
except ImportError:
    mutagen = MutagenFile = None


def _load_enzyme():
    try:
        mod = importlib.import_module("enzyme")
    except ImportError:
        return None
    # enzyme re-parses its bundled Matroska spec XML on every file (~15 ms each); parse it once
    with contextlib.suppress(Exception):
        ebml = importlib.import_module("enzyme.parsers.ebml")
        ebml.get_matroska_specs = functools.lru_cache(maxsize=None)(ebml.get_matroska_specs)
    return mod


enzyme = _load_enzyme()

# ── Constants ─────────────────────────────────────────────────────────────────
VERSION    = "14.0925.17"
SCHEMA     = 2        # audiodata.js layout; player.html refuses other versions
CACHE_VER  = 2        # bump whenever tag extraction changes → one full rescan
AUDIO_EXTS = frozenset({".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wav", ".webm"})
MATROSKA   = frozenset({".webm"})
ART_EXTS   = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})
# Cover file names in priority order; whole words only ("heart.jpg" is not "art")
ART_RES    = tuple(re.compile(p) for p in (
    r"(?<![a-z])cover(?![a-z])", r"(?<![a-z])folder(?![a-z])", r"(?<![a-z])front(?![a-z])",
    r"(?<![a-z])albumart",       r"(?<![a-z])album(?![a-z])",  r"(?<![a-z])artwork(?![a-z])",
    r"(?<![a-z])art(?![a-z])",
))
DATAFILE   = "audiodata.js"
HTMLFILE   = "index.html"
TEMPLATE   = Path(__file__).resolve().with_name("player.html")
STATE_DIR  = ".mugal"                       # cache, report, fallback covers (inside OUTPUT)
MARKERS    = ('<meta name="generator" content="mugal"', "MUSIC PLAYER — file:// compatible")
FIELDS     = ("title", "artist", "album", "albumArtist", "track", "disc", "year", "genre", "duration", "art")
EMPTY_REC  = ("", "", "", "", 0, 0, 0, "", 0.0, 0)

# Default skips (disable with --all). Dot-prefixed names are always skipped too.
SKIP_DIRS = frozenset(n.casefold() for n in (
    # macOS
    ".Trashes", ".Spotlight-V100", ".fseventsd", ".DocumentRevisions-V100", ".TemporaryItems",
    ".AppleDouble", ".AppleDB", ".AppleDesktop", "Network Trash Folder", "Temporary Items",
    # Windows
    "$RECYCLE.BIN", "RECYCLER", "RECYCLED", "System Volume Information", "$WINDOWS.~BT",
    # Linux
    "lost+found",
    # NAS: Synology, QNAP, NetApp
    "@eaDir", "#recycle", "#snapshot", "@Recycle", "@Recently-Snapshot", "@__thumb", ".snapshot",
))
SKIP_SUFFIXES   = (".photoslibrary", ".musiclibrary", ".tvlibrary", ".app", ".bundle")  # macOS packages
ANDROID_PRIVATE = frozenset({("android", "data"), ("android", "obb")})                 # (parent, name)
NOMEDIA         = ".nomedia"                                                           # Android: skip folder
_WIN_HIDDEN     = (stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM) if os.name == "nt" else 0


# ── Console ───────────────────────────────────────────────────────────────────
def info(msg: str = "") -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(f"⚠  {msg}", file=sys.stderr, flush=True)


def die(msg: str, code: int = 1):
    print(f"✗ {msg}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def progress_bar(label: str, total: int = 0):
    """Return tick(n, final=False): a throttled single-line progress display (TTY only)."""
    tty, last = sys.stderr.isatty(), [0.0]

    def tick(n: int, final: bool = False) -> None:
        if not tty:
            return
        now = time.monotonic()
        if not final and now - last[0] < 0.1:
            return
        last[0] = now
        if total:
            w = 30
            f = w * n // total
            sys.stderr.write(f"\r  {label} [{'█' * f}{'░' * (w - f)}] {n}/{total}")
        else:
            sys.stderr.write(f"\r  {label}… {n}")
        sys.stderr.write("\n" if final else "")
        sys.stderr.flush()

    return tick


def fmt_hours(sec: float) -> str:
    return f"{sec / 3600:.0f} h" if sec >= 36000 else f"{sec / 3600:.1f} h" if sec >= 3600 else f"{sec / 60:.0f} min"


def install_hint(*pkgs: str) -> list[str]:
    exe = sys.executable or "python3"
    exe = f'"{exe}"' if " " in exe else exe
    names = " ".join(pkgs)
    if os.environ.get("UV_RUN_RECURSION_DEPTH"):            # running under `uv run`: its env is throwaway
        return ["   Install it by adding it to the uv command:",
                f"     uv run --with {' --with '.join(pkgs)} {Path(__file__).name} …   (or plain `uv run {Path(__file__).name}`)"]
    managed = sys.prefix == sys.base_prefix and Path(sysconfig.get_path("stdlib"), "EXTERNALLY-MANAGED").exists()
    lines = ["   Install with one of:"]
    if managed:
        lines.append(f"     {exe} -m venv .venv && .venv/bin/pip install {names}   (this Python is externally managed)")
    else:
        lines.append(f"     {exe} -m pip install {names}")
    lines.append(f"     uv run {Path(__file__).name} …   (installs everything listed in the script header)")
    return lines


# ── CLI ───────────────────────────────────────────────────────────────────────
def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="mugal",
        description="Scan a music folder and generate a browser player that works from file://.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:" + __doc__.split("Examples:", 1)[1],
    )
    p.add_argument("root", nargs="?", help="music folder to scan (default: current folder)")
    p.add_argument("-o", "--output", metavar="DIR", help="where index.html + audiodata.js go (default: ROOT)")
    p.add_argument("--covers", metavar="DIR",
                   help="save covers extracted from tags to DIR (default: cover.<ext> in each album folder, "
                        f"or OUTPUT/{STATE_DIR}/covers when the folder is read-only)")
    p.add_argument("--no-art", action="store_true", help="skip cover art entirely")
    p.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                   help="skip files/folders matching GLOB, by path relative to ROOT or by name (repeatable)")
    p.add_argument("--all", action="store_true",
                   help="also scan hidden/system folders (.*, $RECYCLE.BIN, @eaDir, .nomedia, Android/data …)")
    p.add_argument("--min-duration", type=float, default=0, metavar="SEC", help="skip tracks shorter than SEC seconds")
    p.add_argument("--force-rescan", action="store_true", help="ignore the scan cache and re-read every file")
    p.add_argument("-j", "--jobs", type=int, default=0, metavar="N",
                   help="parallel tag readers (default 0 = auto: threads only when reads are slow, e.g. NAS)")
    p.add_argument("--no-html", action="store_true", help="only rewrite audiodata.js")
    p.add_argument("-x", "--extract-template", action="store_true", help="only (re)write index.html, skip scanning")
    p.add_argument("--force", action="store_true", help="overwrite an index.html that mugal didn't create")
    p.add_argument("--no-open", action="store_true", help="don't open the browser when done")
    p.add_argument("--serve", nargs="?", const="8000", metavar="[HOST:]PORT",
                   help="serve the player over HTTP for debugging (default 127.0.0.1:8000; no seeking)")
    p.add_argument("-v", "--verbose", action="store_true", help="list every file read and every file skipped")
    p.add_argument("-V", "--version", action="version", version=f"mugal {VERSION}")
    a = p.parse_args(argv)
    if a.jobs < 0:
        p.error("--jobs must be 0 (auto) or more")
    if a.min_duration < 0 or not math.isfinite(a.min_duration):
        p.error("--min-duration must be a non-negative number")
    if a.serve is not None:
        host, _, port = a.serve.rpartition(":")
        try:
            a.serve = (host.strip("[]") or "127.0.0.1", int(port))
        except ValueError:
            p.error(f"--serve expects PORT or HOST:PORT, got {a.serve!r}")
        if not 0 <= a.serve[1] <= 65535:
            p.error("--serve port must be 0–65535")
    return a


# ── Filesystem walk ───────────────────────────────────────────────────────────
def _hidden(entry: os.DirEntry) -> bool:
    if entry.name.startswith("."):
        return True
    if _WIN_HIDDEN:
        with contextlib.suppress(OSError):
            return bool(entry.stat(follow_symlinks=False).st_file_attributes & _WIN_HIDDEN)
    return False


def _matches(rel: str, name: str, patterns: list[str], is_dir: bool = False) -> bool:
    return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(name, p) or (is_dir and fnmatch.fnmatch(rel + "/", p))
               for p in patterns)


def _printable(rel: str) -> str:
    return rel.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def walk(root: Path, excludes: list[str], everything: bool, problems: list, on_dir) -> list[tuple]:
    """
    Iterative scandir walk that prunes skipped/excluded folders before descending,
    follows folder symlinks with loop detection and never raises on unreadable entries.
    Returns [(rel_dir, abs_dir, [(name, size, mtime_ns)], [(image_name, size)])].
    """
    out, seen, stack = [], set(), [("", str(root))]
    while stack:
        rel, path = stack.pop()
        try:
            st = os.stat(path)
            if st.st_ino:                                   # some network filesystems report 0
                key = (st.st_dev, st.st_ino)
                if key in seen:                             # symlink loop or already-visited target
                    continue
                seen.add(key)
            with os.scandir(path) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as e:
            problems.append((rel or ".", f"folder not readable ({e.strerror or e})"))
            continue
        if rel and not everything and any(e.name == NOMEDIA for e in entries):
            continue
        audio, images, subdirs = [], [], []
        parent = rel.rpartition("/")[2].casefold()
        for e in entries:
            name = e.name
            r = f"{rel}/{name}" if rel else name
            if not everything and _hidden(e):
                continue
            try:
                name.encode("utf-8")
            except UnicodeEncodeError:
                problems.append((_printable(r), "name is not valid UTF-8 (rename it to include it)"))
                continue
            try:
                if e.is_dir():
                    cf = name.casefold()
                    if not everything and (cf in SKIP_DIRS or cf.endswith(SKIP_SUFFIXES)
                                           or (parent, cf) in ANDROID_PRIVATE):
                        continue
                    if not (excludes and _matches(r, name, excludes, True)):
                        subdirs.append((r, e.path))
                elif e.is_file():
                    ext = os.path.splitext(name)[1].lower()
                    if ext in AUDIO_EXTS:
                        if not (excludes and _matches(r, name, excludes)):
                            s = e.stat()
                            audio.append((name, s.st_size, s.st_mtime_ns))
                    elif ext in ART_EXTS:
                        images.append((name, e.stat().st_size))
            except OSError as ex:
                problems.append((r, f"not readable ({ex.strerror or ex})"))
        stack.extend(reversed(subdirs))
        if audio or images:
            out.append((rel, path, audio, images))
            on_dir(len(audio))
    return out


# ── Tag reading ───────────────────────────────────────────────────────────────
def _first(v) -> str:
    """First value of a mutagen tag as clean text: ID3 frames, lists, MP4 (n, total) tuples, bytes."""
    if v is None:
        return ""
    genres = getattr(v, "genres", None)                     # ID3 TCON resolves "(17)" → "Rock"
    if genres is not None:
        v = genres
    elif hasattr(v, "text"):
        v = v.text
    if isinstance(v, list):
        if not v:
            return ""
        v = v[0]
    if isinstance(v, tuple):                                # MP4 trkn / disk
        v = v[0] if v else ""
    if isinstance(v, bytes):
        v = v.decode("utf-8", "replace")
    return str(v).replace("\x00", " ").strip()


def _tag(tags, *keys: str) -> str:
    for k in keys:
        try:
            v = _first(tags.get(k))
        except Exception:
            continue
        if v:
            return v
    return ""


def _int(s: str) -> int:
    m = re.match(r"\s*(\d{1,6})", s)
    return int(m.group(1)) if m else 0


_YEAR = re.compile(r"(1[0-9]{3}|20[0-9]{2})")


def _year(s: str) -> int:
    m = _YEAR.search(s)
    return int(m.group(1)) if m else 0


def _seconds(v) -> float:
    try:
        d = float(v or 0)
    except (TypeError, ValueError):
        return 0.0
    return round(d, 1) if math.isfinite(d) and d > 0 else 0.0


def _has_art(mut) -> bool:
    if getattr(mut, "pictures", None):
        return True
    tags = getattr(mut, "tags", None)
    if not tags:
        return False
    try:
        return (any(str(k).startswith("APIC") for k in tags.keys())
                or bool(tags.get("covr")) or bool(tags.get("metadata_block_picture")))
    except Exception:
        return False


def read_mutagen(mut) -> list:
    tags = mut.tags or {}
    return [
        _tag(tags, "TIT2", "title", "\xa9nam"),
        _tag(tags, "TPE1", "artist", "\xa9ART"),
        _tag(tags, "TALB", "album", "\xa9alb"),
        _tag(tags, "TPE2", "albumartist", "album artist", "aART"),
        _int(_tag(tags, "TRCK", "tracknumber", "trkn")),
        _int(_tag(tags, "TPOS", "discnumber", "disk")),
        _year(_tag(tags, "TDRC", "TYER", "TDOR", "date", "year", "originaldate", "\xa9day")),
        _tag(tags, "TCON", "genre", "\xa9gen"),
        _seconds(getattr(getattr(mut, "info", None), "length", 0)),
        int(_has_art(mut)),
    ]


def read_matroska(path: str) -> list:
    """WebM/Matroska tags via enzyme. Untargeted tags (what ffmpeg/yt-dlp write) count as track level."""
    with open(path, "rb") as fh:
        mkv = enzyme.MKV(fh)
    tags: dict[tuple[int, str], str] = {}
    for tag in mkv.tags:
        targets = getattr(tag.targets, "data", tag.targets) or []
        level = next((e.data for e in targets if getattr(e, "name", "") == "TargetTypeValue"), 0)
        for st in tag.simpletags:
            if isinstance(st.string, str) and st.string.strip():
                tags.setdefault((int(level or 0), st.name.upper()), st.string.strip())

    def pick(names, levels=(30, 0, 50)) -> str:
        return next((tags[(lv, n)] for n in names for lv in levels if (lv, n) in tags), "")

    inf = mkv.info
    track_title = tags.get((30, "TITLE"), "")
    title = track_title or tags.get((0, "TITLE")) or (inf.title if inf and inf.title else "") or tags.get((50, "TITLE"), "")
    artist = pick(("ARTIST", "PERFORMER"))
    album_artist = pick(("ALBUM_ARTIST", "ALBUMARTIST")) or (tags.get((50, "ARTIST"), "") if (30, "ARTIST") in tags else "")
    return [
        str(title).strip(),
        artist,
        pick(("ALBUM",)) or (tags.get((50, "TITLE"), "") if track_title else ""),
        album_artist,
        _int(pick(("PART_NUMBER", "TRACKNUMBER", "TRACK"), (30, 0))),
        _int(pick(("DISCNUMBER", "DISC"), (30, 0, 50)) or tags.get((40, "PART_NUMBER"), "")),
        _year(pick(("DATE_RELEASED", "DATE_RECORDED", "DATE", "YEAR"))),
        pick(("GENRE",)),
        _seconds(inf.duration.total_seconds() if inf and inf.duration else 0),
        0,
    ]


def _err(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"[:200]


def matroska_reader() -> bool:
    if enzyme:
        return True
    if MutagenFile:                     # future mutagen releases may add Matroska support
        with contextlib.suppress(Exception):
            return importlib.util.find_spec("mutagen.matroska") is not None
    return False


def has_reader(ext: str) -> bool:
    return matroska_reader() if ext in MATROSKA else MutagenFile is not None


def parse_file(path: str, ext: str) -> tuple[str, list | str]:
    """
    → (source, record). source: "mutagen" | "enzyme" | "err" (record = message) |
      "name"    no reader installed for this format (re-read once one is) |
      "unknown" a reader ran but didn't recognise the format (listed by file name; not retried).
    """
    if MutagenFile:
        try:
            mut = MutagenFile(path)
        except Exception as e:
            return "err", _err(e)
        if mut is not None:
            try:
                return "mutagen", read_mutagen(mut)
            except Exception as e:
                return "err", _err(e)
    if ext in MATROSKA:
        if not enzyme:
            return "name", list(EMPTY_REC)
        try:
            return "enzyme", read_matroska(path)
        except Exception as e:
            return "err", _err(e)
    return ("unknown" if MutagenFile else "name"), list(EMPTY_REC)


def parser_versions() -> dict[str, str | None]:
    return {"mutagen": getattr(mutagen, "version_string", None) if mutagen else None,
            "enzyme": getattr(enzyme, "__version__", "?") if enzyme else None}


def ensure_matroska_reader(count: int) -> None:
    """Offer to install a WebM tag reader when .webm files are present but none is importable."""
    global enzyme
    if not count or matroska_reader():
        return
    warn(f"{count} .webm file{'s' * (count != 1)} found, but no WebM tag reader (enzyme) is installed.\n"
         "   Without it they're listed by file name only: no artist, album or duration.")
    for line in install_hint("enzyme"):
        print(line, file=sys.stderr)
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        print("   Continuing without it (not an interactive terminal).", file=sys.stderr)
        return
    while True:
        try:
            ans = input("   [c]ontinue without it · [r]etry after installing · [a]bort  (c): ").strip().lower()
        except EOFError:
            print(file=sys.stderr)
            ans = "c"
        if ans in ("", "c", "continue"):
            return
        if ans in ("a", "abort", "q", "quit"):
            die("Aborted.")
        if ans in ("r", "retry"):
            importlib.invalidate_caches()
            with contextlib.suppress(Exception):            # a fresh `pip install --user` creates this dir
                site.addsitedir(site.getusersitepackages())
            enzyme = _load_enzyme()
            if enzyme:
                info(f"   ✓ enzyme {getattr(enzyme, '__version__', '')} loaded")
                return
            print(f"   ✗ still not importable by {sys.executable}", file=sys.stderr)


def run_parallel(fn, items: list, jobs: int, on_done) -> None:
    """
    Map fn over items; on_done(item, result) runs on the calling thread.
    jobs=0 (auto): tag parsing is CPU-bound under the GIL, so threads only pay off when reads wait on I/O
    (NAS, HDD, cold cache). Read the first files sequentially and go parallel only if less than half of
    the wall time was CPU time.
    """
    itr = iter(items)
    if jobs == 0:
        free_threaded = not getattr(sys, "_is_gil_enabled", lambda: True)()
        jobs = min(16, (os.cpu_count() or 2) * 2) if free_threaded else 1
        if not free_threaded:
            wall, cpu, n = time.perf_counter(), time.process_time(), 0
            for it in itertools.islice(itr, 200):
                on_done(it, fn(it))
                n += 1
            wall, cpu = time.perf_counter() - wall, time.process_time() - cpu
            if n == 200 and wall > 0.2 and cpu < wall / 2:
                jobs = 8
    if jobs <= 1:
        for it in itr:
            on_done(it, fn(it))
        return
    ex = ThreadPoolExecutor(jobs)
    pending = {}
    try:
        for it in itertools.islice(itr, jobs * 4):
            pending[ex.submit(fn, it)] = it
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for f in done:
                on_done(pending.pop(f), f.result())
                nxt = next(itr, None)
                if nxt is not None:
                    pending[ex.submit(fn, nxt)] = nxt
    finally:
        ex.shutdown(wait=True, cancel_futures=True)


# ── Cache (OUTPUT/.mugal/cache.json) ──────────────────────────────────────────
def load_cache(path: Path) -> dict:
    with contextlib.suppress(OSError, ValueError, AttributeError):
        c = json.loads(path.read_text("utf-8"))
        if c.get("v") == CACHE_VER and isinstance(c.get("files"), dict) and isinstance(c.get("parsers"), dict):
            return c
    return {"v": CACHE_VER, "parsers": {}, "files": {}}


def cache_hit(e, size: int, mtime: int, ext: str, stale: set) -> bool:
    if not (isinstance(e, list) and len(e) == 4 and e[0] == size and e[1] == mtime):
        return False
    src, rec = e[2], e[3]
    if src == "err":
        return isinstance(rec, str) and not stale
    if src in stale or not (isinstance(rec, list) and len(rec) == len(FIELDS)):
        return False
    return not (src == "name" and has_reader(ext))


# ── Cover art ─────────────────────────────────────────────────────────────────
def pick_image(images: list[tuple[str, int]]) -> str | None:
    def prio(name: str) -> int:
        stem = os.path.splitext(name)[0].lower()
        return next((i for i, rx in enumerate(ART_RES) if rx.search(stem)), len(ART_RES))
    best = min(images, key=lambda im: (prio(im[0]), -im[1], im[0]), default=None)  # name, then largest
    return best[0] if best else None


def _sniff(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:4] == b"GIF8":
        return ".gif"
    return None


def embedded_art(path: str) -> tuple[bytes, str] | None:
    """(image bytes, extension) of the embedded front cover (or first picture), or None."""
    try:
        mut = MutagenFile(path)
        pics: list[tuple[int, bytes]] = []                  # (picture type, data); 3 = front cover
        tags = getattr(mut, "tags", None)
        if tags:
            for k in tags.keys():
                if str(k).startswith("APIC"):
                    a = tags[k]
                    pics.append((getattr(a, "type", 0), a.data))
            for c in tags.get("covr") or []:
                pics.append((3, bytes(c)))
            for b in tags.get("metadata_block_picture") or []:
                from mutagen.flac import Picture
                p = Picture(base64.b64decode(b))
                pics.append((p.type, p.data))
        for p in getattr(mut, "pictures", None) or []:
            pics.append((p.type, p.data))
        pics.sort(key=lambda p: p[0] != 3)
        for _, data in pics:
            ext = _sniff(data or b"")
            if ext and len(data) < 32 << 20:
                return data, ext
    except Exception:
        pass
    return None


def resolve_art(folders: dict[str, list[tuple[str, int]]], images: dict, root: Path,
                covers: Path | None, fallback: Path, problems: list) -> dict[str, Path]:
    """
    One cover per track folder, by priority:
      1. an image file in the folder (cover > folder > front > albumart > album > artwork > art > any; largest wins)
      2. art embedded in one of its tracks, extracted once: to `covers` if given, else cover.<ext> in the
         folder, else (read-only folder/drive) to `fallback`
      3. the nearest ancestor folder's cover (not the library root's)
    `folders` maps rel folder → [(abs track path, has_art)].
    """
    art: dict[str, Path] = {}
    ro_devs: set[int] = set()
    told = False
    for rel in sorted(folders, key=lambda f: f.count("/") + (f != "")):   # parents before children
        folder = root / rel if rel else root
        img = pick_image(images.get(rel, []))
        if img:
            art[rel] = folder / img
            continue
        src = next((p for p, has in folders[rel] if has), None)
        if src and MutagenFile:
            got = _extract(src, rel, folder, covers, fallback, ro_devs)
            if isinstance(got, tuple):
                art[rel] = got[0]
                if got[1] and got[0].parent == fallback and not told:
                    info(f"ℹ  Read-only folder(s): extracted covers go to {fallback}")
                    told = True
                continue
            if got:
                problems.append((rel or ".", got))
        parts = rel.split("/")
        for i in range(len(parts) - 1, 0, -1):              # ancestors, excluding the root
            anc = "/".join(parts[:i])
            hit = art.get(anc) or ((root / anc / ai) if (ai := pick_image(images.get(anc, []))) else None)
            if hit:
                art[rel] = hit
                break
    return art


def _extract(track: str, rel: str, folder: Path, covers: Path | None, fallback: Path, ro_devs: set):
    """Write the embedded cover of `track`; → (Path, newly written?) | error message | None (no art)."""
    key = hashlib.blake2b(rel.encode("utf-8", "surrogateescape"), digest_size=8).hexdigest()
    for d in filter(None, (covers, fallback)):              # reuse a cover extracted on an earlier run
        for ext in (".jpg", ".png", ".webp", ".gif"):
            if (d / f"{key}{ext}").is_file():
                return d / f"{key}{ext}", False
    emb = embedded_art(track)
    if not emb:
        return None
    data, ext = emb
    targets = [covers / f"{key}{ext}"] if covers else [folder / f"cover{ext}", fallback / f"{key}{ext}"]
    last = None
    for target in targets:
        if target.parent == folder and target.exists():     # never overwrite the user's files
            return target, False
        with contextlib.suppress(OSError):
            if target.parent == folder and folder.stat().st_dev in ro_devs:
                continue
        try:
            write_atomic(target, data)
            return target, True
        except OSError as e:
            if e.errno == errno.EROFS:
                with contextlib.suppress(OSError):
                    ro_devs.add(folder.stat().st_dev)
            last = f"cover not saved ({e.strerror or e})"
    return last


# ── Output ────────────────────────────────────────────────────────────────────
def write_atomic(path: Path, data: str | bytes) -> None:
    """Write via temp file + rename so readers never see a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        um = os.umask(0)
        os.umask(um)
        mode = 0o666 & ~um
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data.encode("utf-8") if isinstance(data, str) else data)
        os.chmod(tmp, mode)                                 # mkstemp is 0600; web servers need to read it
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:                         # Windows: target briefly locked
                if attempt == 4:
                    raise
                time.sleep(0.2)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def href(target: Path, out_dir: Path) -> str:
    """URL of target as seen from index.html in out_dir: relative POSIX path, or file:// URI across drives."""
    try:
        return Path(os.path.relpath(target, out_dir)).as_posix()
    except ValueError:
        return target.as_uri()


_DIGITS = re.compile(r"(\d+)")
_MARKS = dict.fromkeys(range(0x300, 0x370))                 # combining diacritics


def natural(s: str) -> tuple:
    """Case/accent-insensitive natural sort key: 'Track 2' < 'track 10' < 'Über'."""
    parts = _DIGITS.split(unicodedata.normalize("NFKD", s.casefold()).translate(_MARKS))
    parts[1::2] = [int(p) for p in parts[1::2]]
    return tuple(parts)


@functools.lru_cache(maxsize=None)
def folder_key(rel: str) -> tuple:
    # raw name as tie-break: "Abc" and "abc" (distinct folders on Linux) must not interleave,
    # the player relies on every folder subtree being one contiguous range
    return tuple((natural(p), p) for p in rel.split("/")) if rel else ()


def build_payload(entries: list, art: dict[str, Path], root: Path, out_dir: Path) -> dict:
    """
    Compact layout (schema 2). Tracks are sorted depth-first by folder, then disc, track, file name,
    so every folder subtree is one contiguous range. Repeated strings live once in `strings`.
      folders: [rel path]            art: [strings index per folder, 0 = none]
      tracks:  [folder, file, title ("" = file stem), artist, album, albumArtist (0 = artist),
                track, disc, year, genre, duration]   (artist/album/albumArtist/genre = strings index)
    """
    entries.sort(key=lambda e: (folder_key(e[0]), e[2][5], e[2][4], natural(e[1])))
    folders = list(dict.fromkeys(e[0] for e in entries))
    fidx = {f: i for i, f in enumerate(folders)}
    strings: dict[str, int] = {"": 0}
    s = lambda v: strings.setdefault(v, len(strings))       # noqa: E731
    rows = []
    for rel, name, r in entries:
        title, artist, album, aa, track, disc, year, genre, dur, _ = r
        rows.append([fidx[rel], name, "" if title == os.path.splitext(name)[0] else title,
                     s(artist), s(album), s("" if aa == artist else aa), track, disc, year, s(genre), dur])
    art_col = [s(href(art[f], out_dir)) if f in art else 0 for f in folders]
    base = "" if root == out_dir else href(root, out_dir).rstrip("/") + "/"
    body = {"folders": folders, "art": art_col, "strings": list(strings), "tracks": rows}
    digest = hashlib.blake2b(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                             digest_size=6).hexdigest()
    return {"schema": SCHEMA, "version": digest, "generated": int(time.time()), "root": str(root),
            "base": "" if base == "./" else base, "count": len(rows), **body}


def write_datafile(payload: dict, out_dir: Path) -> int:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    # JSON.parse of a string literal parses ~1.7× faster than the equivalent object literal
    js = f"window.__AUDIO_DATA=JSON.parse({json.dumps(body, ensure_ascii=False)});\n"
    out = out_dir / DATAFILE
    write_atomic(out, js)
    return out.stat().st_size


def write_html(out_dir: Path, force: bool) -> bool:
    try:
        tpl = TEMPLATE.read_text("utf-8")
    except OSError as e:
        die(f"player template not found: {TEMPLATE} ({e.strerror or e}) — keep player.html next to mugal.py")
    out = out_dir / HTMLFILE
    if out.exists() and not force:
        try:
            current = out.read_text("utf-8", errors="replace")[:400_000]
        except OSError:
            current = ""
        if not any(m in current for m in MARKERS):
            warn(f"{out} exists and wasn't made by mugal — left untouched (use --force to overwrite)")
            return False
    write_atomic(out, tpl)
    return True


def write_report(out_dir: Path, problems: list) -> Path | None:
    path = out_dir / STATE_DIR / "scan-report.txt"
    if not problems:
        with contextlib.suppress(OSError):
            path.unlink()
        return None
    lines = [f"mugal scan report — {time.strftime('%Y-%m-%d %H:%M')}", ""]
    lines += [f"{reason}\t{p}" for p, reason in sorted(problems, key=lambda x: (x[1], x[0]))]
    with contextlib.suppress(OSError):
        write_atomic(path, "\n".join(lines) + "\n")
        return path
    return None


# ── Scan ──────────────────────────────────────────────────────────────────────
def scan(a, root: Path, out_dir: Path, covers: Path | None) -> tuple[dict, dict]:
    t0 = time.monotonic()
    problems: list[tuple[str, str]] = []
    found = 0
    tick = progress_bar("Finding audio files")

    def on_dir(n: int) -> None:
        nonlocal found
        found += n
        tick(found)

    dirs = walk(root, a.exclude, a.all, problems, on_dir)
    tick(found, final=True)
    files = [(rel, path, name, size, mtime) for rel, path, audio, _ in dirs for name, size, mtime in audio]
    images = {rel: imgs for rel, _, _, imgs in dirs if imgs}
    if not files:
        die(f"No audio files found in {root} — nothing written.")

    ensure_matroska_reader(sum(os.path.splitext(f[2])[1].lower() in MATROSKA for f in files))

    cache_path = out_dir / STATE_DIR / "cache.json"
    cache = {"v": CACHE_VER, "parsers": {}, "files": {}} if a.force_rescan else load_cache(cache_path)
    now = parser_versions()
    stale = {k for k, v in cache["parsers"].items() if now.get(k) and now[k] != v}
    old, fresh, todo = cache["files"], {}, []
    for rel, path, name, size, mtime in files:
        key = f"{rel}/{name}" if rel else name
        ext = os.path.splitext(name)[1].lower()
        if cache_hit(old.get(key), size, mtime, ext, stale):
            fresh[key] = old[key]
        else:
            todo.append((key, os.path.join(path, name), ext, size, mtime))
    cache["files"] = old = None                             # free the previous cache early

    tick = progress_bar("Reading tags", len(todo))
    done = 0

    def on_done(item, res) -> None:
        nonlocal done
        fresh[item[0]] = [item[3], item[4], res[0], res[1]]
        done += 1
        if a.verbose:
            info(f"  {'✗' if res[0] == 'err' else '·'} {_printable(item[0])}")
        else:
            tick(done)

    gc.freeze()       # keep the GC from re-traversing the (100k-entry) cache on every collection: ~2× faster
    try:
        run_parallel(lambda it: parse_file(it[1], it[2]), todo, a.jobs, on_done)
    finally:
        gc.unfreeze()
        if todo and not a.verbose:
            tick(done, final=True)
        with contextlib.suppress(OSError):                  # keep progress even when interrupted
            write_atomic(cache_path, json.dumps(
                {"v": CACHE_VER, "parsers": {**cache["parsers"], **{k: v for k, v in now.items() if v}},
                 "files": fresh}, ensure_ascii=False, separators=(",", ":")))

    entries, track_folders = [], {}
    unread = no_reader = short = 0
    for rel, path, name, _, _ in files:
        key = f"{rel}/{name}" if rel else name
        src, rec = fresh[key][2], fresh[key][3]
        if src == "err":
            unread += 1
            problems.append((_printable(key), f"unreadable, skipped ({rec})"))
            continue
        if src == "name":
            no_reader += 1
            if MutagenFile:                                 # without mutagen every file is "name": no spam
                problems.append((_printable(key), "no tag reader installed, listed by file name"))
        if a.min_duration and 0 < rec[8] < a.min_duration:
            short += 1
            continue
        entries.append((rel, name, rec))
        track_folders.setdefault(rel, []).append((os.path.join(path, name), rec[9]))

    art = {} if a.no_art else resolve_art(track_folders, images, root, covers,
                                          out_dir / STATE_DIR / "covers", problems)
    stats = {"files": len(files), "parsed": len(todo), "unread": unread, "no_reader": no_reader,
             "short": short, "problems": problems, "secs": time.monotonic() - t0,
             "duration": sum(e[2][8] for e in entries)}
    return build_payload(entries, art, root, out_dir), stats


# ── Serve (debug helper) ──────────────────────────────────────────────────────
def serve(out_dir: Path, root: Path, covers: Path | None, host: str, port: int, open_browser: bool,
          verbose: bool) -> None:
    import http.server
    import urllib.parse

    try:
        top = Path(os.path.commonpath([str(p) for p in (out_dir, root, covers) if p]))
    except ValueError:
        die("--serve needs ROOT, OUTPUT and --covers on the same drive")
    sub = out_dir.relative_to(top).as_posix()
    url_path = "/" + urllib.parse.quote(f"{sub}/" if sub != "." else "") + HTMLFILE

    class Handler(http.server.SimpleHTTPRequestHandler):
        extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                          ".js": "text/javascript; charset=utf-8", ".mp3": "audio/mpeg", ".flac": "audio/flac",
                          ".ogg": "audio/ogg", ".opus": "audio/ogg", ".m4a": "audio/mp4", ".aac": "audio/aac",
                          ".wav": "audio/wav", ".webm": "audio/webm", ".webp": "image/webp"}

        def end_headers(self):
            self.send_header("Cache-Control", "no-cache")
            super().end_headers()

        def log_message(self, fmt, *args):
            if verbose:
                super().log_message(fmt, *args)

        def handle(self):
            with contextlib.suppress(ConnectionError):      # browsers abort media requests constantly
                super().handle()

    try:
        httpd = http.server.ThreadingHTTPServer((host, port), functools.partial(Handler, directory=str(top)))
    except OSError as e:
        die(f"can't listen on {host}:{port} ({e.strerror or e})")
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0", "::", "") else host
    url = f"http://{shown}:{httpd.server_port}{url_path}"
    info(f"\nServing {top}\n  {url}\n  (debug helper: no HTTP range support, so seeking is disabled · Ctrl-C stops)")
    if host not in ("127.0.0.1", "localhost", "::1"):
        warn(f"listening on {host}: everything under {top} is readable from your network")
    if open_browser:
        open_url(url)
    with httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            info("\nStopped.")


def open_url(url: str) -> None:
    with contextlib.suppress(Exception):
        webbrowser.open_new_tab(url)


# ── Main ──────────────────────────────────────────────────────────────────────
def main(argv=None) -> int:
    for s in (sys.stdout, sys.stderr):                      # never crash on a console that can't print ✓/█
        with contextlib.suppress(Exception):
            s.reconfigure(errors="replace")
    a = parse_args(argv)
    root = Path(a.root).expanduser().resolve() if a.root else Path.cwd().resolve()
    if not root.is_dir():
        die(f"Not a folder: {root}")
    out_dir = Path(a.output).expanduser().resolve() if a.output else root
    covers = Path(a.covers).expanduser().resolve() if a.covers else None
    if out_dir.exists() and not out_dir.is_dir():         # created on first write, so an abort leaves nothing
        die(f"Output is not a folder: {out_dir}")
    page = out_dir / HTMLFILE

    if a.extract_template:
        if write_html(out_dir, a.force):
            info(f"✓ Wrote {page}")
        if a.serve:
            serve(out_dir, root, covers, *a.serve, not a.no_open, a.verbose)
        return 0

    info(f"mugal {VERSION}\nRoot   : {root}\nOutput : {out_dir}" + (f"\nExclude: {', '.join(a.exclude)}" if a.exclude else ""))
    if not MutagenFile:
        warn("mutagen is not installed — tracks are listed by file name only (no tags, durations or covers).")
        for line in install_hint("mutagen", "enzyme"):
            print(line, file=sys.stderr)

    payload, st = scan(a, root, out_dir, covers)
    size = write_datafile(payload, out_dir)
    cached = st["files"] - st["parsed"]
    info(f"✓ {st['files']} audio files in {st['secs']:.1f}s ({cached} cached, {st['parsed']} read)")
    mb = f"{size / 1048576:.1f} MB" if size >= 1048576 else f"{max(1, size // 1024)} KB"
    info(f"✓ {payload['count']} tracks · {fmt_hours(st['duration'])} · {DATAFILE} {mb}")
    if st["unread"]:
        warn(f"{st['unread']} unreadable file{'s' * (st['unread'] != 1)} skipped (corrupt or truncated)")
    if st["no_reader"]:
        warn(f"{st['no_reader']} file{'s' * (st['no_reader'] != 1)} listed by file name only (no tag reader)")
    if st["short"]:
        info(f"ℹ  {st['short']} track{'s' * (st['short'] != 1)} shorter than {a.min_duration:g}s skipped")
    other = len(st["problems"]) - st["unread"] - st["no_reader"]
    if other > 0:
        warn(f"{other} other problem{'s' * (other != 1)} (unreadable folders, invalid names, covers)")
    report = write_report(out_dir, st["problems"])
    if report:
        info(f"   Details: {report}" + ("" if a.verbose else "  (or run with -v)"))
        if a.verbose:
            for p, reason in st["problems"]:
                info(f"   {reason}: {p}")

    html_ok = not a.no_html and write_html(out_dir, a.force)
    if html_ok:
        info(f"✓ Wrote {HTMLFILE}")
    if a.serve:
        serve(out_dir, root, covers, *a.serve, not a.no_open, a.verbose)
    elif page.exists():
        info(f"\nOpen {page.as_uri()}")
        if html_ok and not a.no_open:
            open_url(page.as_uri())
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        sys.exit(130)
