#!/usr/bin/env python3
"""
scan_music.py — Local music library scanner
Generates audiodata.js + index.html for file:// playback.

Usage:
    python scan_music.py [ROOT] [options]

    ROOT defaults to the current working directory.
"""

import argparse
import base64
import fnmatch
import hashlib
import json
import re
import sys
import time
import webbrowser
from pathlib import Path

# deps
try:
    from mutagen import File as MutagenFile
    from mutagen.id3 import ID3NoHeaderError
    HAS_MUTAGEN = True
except ImportError:
    HAS_MUTAGEN = False

try:
    from PIL import Image
    import io as _io
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# ── Constants ─────────────────────────────────────────────────────────────────
AUDIO_EXTS = {".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wav", ".webm"}
ART_NAMES  = {"cover", "folder", "album", "front", "artwork", "art"}
ART_EXTS   = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
THUMB_SIZE = (80, 80)   # px for embedded base64 thumbnails
DATAFILE   = "audiodata.js"
HTMLFILE   = "index.html"

# ── CLI ───────────────────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(
        description="Scan a music folder and generate a self-contained browser player.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "root", nargs="?", default=None,
        help="Root music folder to scan (default: current folder)"
    )
    p.add_argument(
        "-o", "--output", default=None,
        help="Output folder for index.html + audiodata.js (default: same as ROOT)"
    )
    p.add_argument(
        "--no-art", action="store_true",
        help="Skip embedding base64 album art thumbnails (smaller audiodata.js)"
    )
    p.add_argument(
        "--force-rescan", action="store_true",
        help="Ignore any cached state and re-scan all files"
    )
    p.add_argument(
        "--exclude", action="append", default=[], metavar="PATTERN",
        help="Glob pattern to exclude (can repeat). E.g. --exclude '*.wav' --exclude 'Podcasts/*'"
    )
    p.add_argument(
        "--no-html", action="store_true",
        help="Only regenerate audiodata.js, skip writing index.html"
    )
    p.add_argument(
        "--thumb-size", type=int, default=80, metavar="PX",
        help="Thumbnail pixel size (square, default 80)"
    )
    p.add_argument(
        "--min-duration", type=float, default=0, metavar="SEC",
        help="Skip tracks shorter than this many seconds"
    )
    p.add_argument(
        "-v", "--verbose", action="store_true",
        help="Print each scanned file"
    )
    return p.parse_args()

# ── Helpers ───────────────────────────────────────────────────────────────────
def is_excluded(path: Path, root: Path, patterns: list[str]) -> bool:
    rel = str(path.relative_to(root)).replace("\\", "/")
    return any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(path.name, pat)
               for pat in patterns)

def find_art(folder: Path) -> Path | None:
    """Return the best cover image in folder, or None."""
    candidates = []
    for f in folder.iterdir():
        if f.is_file() and f.suffix.lower() in ART_EXTS:
            stem = f.stem.lower()
            priority = next((i for i, n in enumerate(ART_NAMES) if n in stem), 99)
            candidates.append((priority, f))
    if candidates:
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1]
    return None

def image_to_b64(path: Path, size: tuple[int,int]) -> str | None:
    if not HAS_PIL:
        # Fallback: raw file without resize
        try:
            data = path.read_bytes()
            mime = "image/jpeg" if path.suffix.lower() in (".jpg",".jpeg") else "image/png"
            return f"data:{mime};base64,{base64.b64encode(data).decode()}"
        except Exception:
            return None
    try:
        img = Image.open(path).convert("RGB")
        img.thumbnail(size, Image.LANCZOS)
        buf = _io.BytesIO()
        img.save(buf, "JPEG", quality=72, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None

def extract_art_from_tags(mut) -> str | None:
    """Try to pull embedded art from mutagen tags."""
    if not HAS_PIL:
        return None
    try:
        # ID3 APIC
        if hasattr(mut, "tags") and mut.tags:
            for key in mut.tags.keys():
                if key.startswith("APIC"):
                    apic = mut.tags[key]
                    img = Image.open(_io.BytesIO(apic.data)).convert("RGB")
                    img.thumbnail(THUMB_SIZE, Image.LANCZOS)
                    buf = _io.BytesIO()
                    img.save(buf, "JPEG", quality=72, optimize=True)
                    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        # FLAC / Vorbis pictures
        if hasattr(mut, "pictures") and mut.pictures:
            pic = mut.pictures[0]
            img = Image.open(_io.BytesIO(pic.data)).convert("RGB")
            img.thumbnail(THUMB_SIZE, Image.LANCZOS)
            buf = _io.BytesIO()
            img.save(buf, "JPEG", quality=72, optimize=True)
            return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        pass
    return None

def get_tag(mut, *keys, default="") -> str:
    """Pull first available tag value as a clean string."""
    if not mut or not mut.tags:
        return default
    for key in keys:
        try:
            val = mut.tags.get(key)
            if val is None:
                continue
            if hasattr(val, "text"):          # ID3 text frame
                v = str(val.text[0]) if val.text else ""
            elif isinstance(val, list):
                v = str(val[0]) if val else ""
            else:
                v = str(val)
            v = v.strip()
            if v:
                return v
        except Exception:
            continue
    return default

def get_int_tag(mut, *keys, default=0) -> int:
    raw = get_tag(mut, *keys, default="")
    # Handle "3/12" track number format
    if raw:
        raw = raw.split("/")[0].strip()
        try:
            return int(raw)
        except ValueError:
            pass
    return default

def scan_file(path: Path, root: Path, embed_art: bool, thumb_size: int,
              folder_art_cache: dict, min_duration: float) -> dict | None:
    """Return track metadata dict or None if not audio / too short."""
    rel = path.relative_to(root)
    rel_str = str(rel).replace("\\", "/")

    if not HAS_MUTAGEN:
        # Bare minimum without mutagen
        return {
            "path": rel_str,
            "title": path.stem,
            "artist": "",
            "album": str(rel.parent).replace("\\", "/"),
            "albumArtist": "",
            "track": 0,
            "disc": 0,
            "year": "",
            "genre": "",
            "duration": 0,
            "art": None,
            "folder": str(rel.parent).replace("\\", "/"),
        }

    try:
        mut = MutagenFile(path, easy=False)
    except Exception:
        return None

    if mut is None:
        return None

    duration = getattr(mut, "info", None) and getattr(mut.info, "length", 0) or 0
    if duration < min_duration:
        return None

    folder_rel = str(rel.parent).replace("\\", "/")

    # ── Tags ──────────────────────────────────────────────────────────────────
    # Try easy tags first (mutagen easy=False, so we handle both ID3 and Vorbis)
    title       = get_tag(mut, "TIT2", "title",       "\xa9nam", "TITLE",       default=path.stem)
    artist      = get_tag(mut, "TPE1", "artist",      "\xa9ART", "ARTIST",      default="")
    album       = get_tag(mut, "TALB", "album",       "\xa9alb", "ALBUM",       default=folder_rel)
    album_artist= get_tag(mut, "TPE2", "albumartist", "aART",    "ALBUMARTIST", default=artist)
    year        = get_tag(mut, "TDRC", "date",        "\xa9day", "DATE",        "YEAR", default="")
    genre       = get_tag(mut, "TCON", "genre",       "\xa9gen", "GENRE",       default="")
    track_no    = get_int_tag(mut, "TRCK", "tracknumber", "trkn", "TRACKNUMBER")
    disc_no     = get_int_tag(mut, "TPOS", "discnumber", "disk",  "DISCNUMBER")

    # Normalise year to 4-digit string
    year = re.sub(r"[^\d].*", "", str(year))[:4] if year else ""

    # ── Art ───────────────────────────────────────────────────────────────────
    art = None
    if embed_art:
        ts = (thumb_size, thumb_size)
        THUMB_SIZE = ts
        # 1. embedded tags
        art = extract_art_from_tags(mut)
        # 2. folder image (cached per folder)
        if not art:
            if folder_rel not in folder_art_cache:
                art_file = find_art(path.parent)
                folder_art_cache[folder_rel] = image_to_b64(art_file, ts) if art_file else None
            art = folder_art_cache[folder_rel]

    return {
        "path":        rel_str,
        "title":       title,
        "artist":      artist,
        "album":       album,
        "albumArtist": album_artist,
        "track":       track_no,
        "disc":        disc_no,
        "year":        year,
        "genre":       genre,
        "duration":    round(duration, 2),
        "art":         art,
        "folder":      folder_rel,
    }

# ── Tree builder ──────────────────────────────────────────────────────────────
def build_folder_tree(tracks: list[dict]) -> dict:
    """Build nested folder structure from flat track list."""
    tree = {}
    for i, t in enumerate(tracks):
        parts = t["folder"].split("/") if t["folder"] else [""]
        node = tree
        for part in parts:
            if part not in node:
                node[part] = {"__tracks__": [], "__children__": {}}
            node = node[part]["__children__"] if part else node
            if part:
                node = tree
                # rebuild reference
                cur = tree
                for p in parts:
                    cur = cur[p]["__children__"]
                break
        # simpler flat approach: just record unique folders
    # Return sorted unique folder paths
    folders = sorted(set(t["folder"] for t in tracks))
    return folders

# ── Main scan ─────────────────────────────────────────────────────────────────
def scan(root: Path, args) -> list[dict]:
    tracks = []
    folder_art_cache = {}
    all_files = sorted(root.rglob("*"))
    audio_files = [f for f in all_files
                   if f.is_file()
                   and f.suffix.lower() in AUDIO_EXTS
                   and not is_excluded(f, root, args.exclude)]

    total = len(audio_files)
    if not total:
        print("⚠  No audio files found.", file=sys.stderr)
        return []

    print(f"Scanning {total} audio files…")
    t0 = time.time()

    for i, f in enumerate(audio_files):
        if args.verbose:
            print(f"  [{i+1}/{total}] {f.relative_to(root)}")
        else:
            pct = int(50 * (i + 1) / total)
            bar = "█" * pct + "░" * (50 - pct)
            print(f"\r  [{bar}] {i+1}/{total}", end="", flush=True)

        track = scan_file(
            f, root,
            embed_art=not args.no_art,
            thumb_size=args.thumb_size,
            folder_art_cache=folder_art_cache,
            min_duration=args.min_duration,
        )
        if track:
            tracks.append(track)

    elapsed = time.time() - t0
    print(f"\n✓ Scanned {len(tracks)} tracks in {elapsed:.1f}s")
    return tracks

# ── Write audiodata.js ────────────────────────────────────────────────────────
def write_datafile(tracks: list[dict], out_dir: Path, root: Path) -> str:
    """Write audiodata.js and return its version hash."""
    payload = {
        "version":   hashlib.md5(
            json.dumps([t["path"] for t in tracks]).encode()
        ).hexdigest()[:12],
        "generated": int(time.time()),
        "root":      str(root),
        "count":     len(tracks),
        "tracks":    tracks,
    }
    js_body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    out = out_dir / DATAFILE
    out.write_text(f"window.__AUDIO_DATA={js_body};", encoding="utf-8")
    size_kb = out.stat().st_size / 1024
    print(f"✓ Wrote {DATAFILE} ({size_kb:.0f} KB)")
    return payload["version"]

# ── Write index.html (embedded, no external deps) ────────────────────────────
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Music</title>
<style>
/* ── Reset & tokens ─────────────────────────────────────────────────────── */
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:        #0D0F14;
  --bg2:       #13161E;
  --bg3:       #1A1D26;
  --panel:     #111318;
  --border:    #23262F;
  --text:      #E0DAD0;
  --text2:     #8A8480;
  --text3:     #55524E;
  --accent:    #C8873A;
  --accent2:   #9B5E1E;
  --playing:   #3FC87A;
  --hover:     rgba(200,135,58,.08);
  --sel:       rgba(200,135,58,.14);
  --radius:    6px;
  --sans:      'Inter',-apple-system,sans-serif;
  --mono:      'JetBrains Mono','Fira Mono',monospace;
  --now-h:     56px;
  --tree-w:    260px;
}
html,body{height:100%;overflow:hidden;background:var(--bg);color:var(--text);font-family:var(--sans);font-size:14px;line-height:1.4}

/* ── Layout ─────────────────────────────────────────────────────────────── */
#app{display:flex;flex-direction:column;height:100vh}
#topbar{display:flex;align-items:center;gap:10px;padding:10px 14px;background:var(--panel);border-bottom:1px solid var(--border);flex-shrink:0;z-index:10}
#main{display:flex;flex:1;overflow:hidden}
#sidebar{width:var(--tree-w);min-width:180px;max-width:420px;background:var(--panel);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden;flex-shrink:0;user-select:none}
#resizer{width:4px;background:transparent;cursor:col-resize;flex-shrink:0;transition:background .15s}
#resizer:hover,#resizer.dragging{background:var(--accent)}
#tracklist{flex:1;overflow-y:auto;background:var(--bg)}
#nowbar{height:var(--now-h);background:var(--panel);border-top:1px solid var(--border);display:flex;align-items:center;gap:12px;padding:0 16px;flex-shrink:0}

/* ── Topbar ─────────────────────────────────────────────────────────────── */
#logo{font-family:var(--mono);font-size:13px;color:var(--accent);letter-spacing:.08em;white-space:nowrap;margin-right:4px}
#search{flex:1;background:var(--bg3);border:1px solid var(--border);border-radius:var(--radius);color:var(--text);padding:6px 10px;font-size:13px;outline:none;font-family:var(--sans);min-width:0}
#search:focus{border-color:var(--accent)}
#search::placeholder{color:var(--text3)}
#stats{font-size:11px;color:var(--text3);white-space:nowrap;font-family:var(--mono)}
#settingsbtn{background:none;border:none;cursor:pointer;color:var(--text2);font-size:16px;padding:4px 6px;border-radius:var(--radius)}
#settingsbtn:hover{color:var(--text);background:var(--bg3)}

/* ── Settings popover ───────────────────────────────────────────────────── */
#settingspop{position:fixed;top:44px;right:14px;background:var(--bg3);border:1px solid var(--border);border-radius:var(--radius);padding:12px 14px;z-index:100;display:none;min-width:220px;box-shadow:0 8px 32px rgba(0,0,0,.6)}
#settingspop.open{display:block}
#settingspop h3{font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:.1em;margin-bottom:8px}
.sp-row{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;gap:8px}
.sp-row span{font-size:12px;color:var(--text2)}
.sp-btn{background:var(--bg);border:1px solid var(--border);color:var(--text);font-size:11px;padding:4px 10px;border-radius:var(--radius);cursor:pointer}
.sp-btn:hover{border-color:var(--accent);color:var(--accent)}
.sp-btn.danger:hover{border-color:#c84040;color:#c84040}
#idb-status{font-size:10px;color:var(--text3);font-family:var(--mono);margin-top:8px;border-top:1px solid var(--border);padding-top:8px}

/* ── Sidebar tree ───────────────────────────────────────────────────────── */
#tree-header{padding:10px 12px 6px;font-size:10px;color:var(--text3);letter-spacing:.1em;text-transform:uppercase;flex-shrink:0}
#tree{flex:1;overflow-y:auto;padding-bottom:8px}
.tn{display:flex;align-items:center;gap:5px;padding:4px 10px 4px;cursor:pointer;border-radius:4px;margin:1px 4px;font-size:12px;color:var(--text2);transition:background .1s}
.tn:hover{background:var(--hover);color:var(--text)}
.tn.active{background:var(--sel);color:var(--accent)}
.tn .arrow{width:12px;text-align:center;color:var(--text3);font-size:9px;flex-shrink:0;transition:transform .15s}
.tn.open .arrow{transform:rotate(90deg)}
.tn .icon{flex-shrink:0;opacity:.6;font-size:11px}
.tn .label{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:var(--mono);font-size:11px}
.tn .cnt{font-size:9px;color:var(--text3);flex-shrink:0}
.tc{padding-left:16px;overflow:hidden}
.tc.collapsed{display:none}

/* ── Track list ─────────────────────────────────────────────────────────── */
#tracklist-header{display:grid;grid-template-columns:28px 1fr 160px 100px 50px;gap:0;padding:6px 14px;position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--border);font-size:10px;color:var(--text3);letter-spacing:.08em;text-transform:uppercase;z-index:5}
.tr{display:grid;grid-template-columns:28px 1fr 160px 100px 50px;gap:0;padding:5px 14px;cursor:pointer;border-radius:4px;margin:1px 4px;transition:background .08s;align-items:center}
.tr:hover{background:var(--hover)}
.tr.playing{background:rgba(63,200,122,.07)}
.tr.playing .t-title{color:var(--playing)}
.tr.selected{background:var(--sel)}
.tr .t-num{font-size:11px;color:var(--text3);font-family:var(--mono);text-align:right;padding-right:8px}
.tr.playing .t-num{color:var(--playing)}
.t-info{display:flex;flex-direction:column;overflow:hidden;gap:1px}
.t-title{font-size:13px;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.t-sub{font-size:11px;color:var(--text2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.t-artist{font-size:12px;color:var(--text2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.t-album{font-size:11px;color:var(--text3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:var(--mono)}
.t-dur{font-size:11px;color:var(--text3);font-family:var(--mono);text-align:right}
em.hl{color:var(--accent);font-style:normal}

/* ── Now playing bar ────────────────────────────────────────────────────── */
#np-art{width:36px;height:36px;border-radius:3px;background:var(--bg3);flex-shrink:0;overflow:hidden;display:flex;align-items:center;justify-content:center}
#np-art img{width:100%;height:100%;object-fit:cover}
#np-art .placeholder{font-size:18px;opacity:.3}
#np-info{flex:1;min-width:0}
#np-title{font-size:13px;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#np-artist{font-size:11px;color:var(--text2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#controls{display:flex;align-items:center;gap:4px;flex-shrink:0}
.ctrl{background:none;border:none;color:var(--text2);cursor:pointer;padding:6px;border-radius:var(--radius);font-size:16px;transition:color .1s}
.ctrl:hover{color:var(--text)}
.ctrl.active{color:var(--accent)}
#scrubber-wrap{flex:1;display:flex;align-items:center;gap:8px;min-width:0;max-width:320px}
#time-cur,#time-tot{font-size:10px;color:var(--text3);font-family:var(--mono);width:32px;flex-shrink:0}
#time-tot{text-align:right}
#scrubber{flex:1;-webkit-appearance:none;appearance:none;height:3px;border-radius:2px;background:var(--bg3);outline:none;cursor:pointer}
#scrubber::-webkit-slider-thumb{-webkit-appearance:none;width:10px;height:10px;border-radius:50%;background:var(--accent);cursor:pointer}
#vol-wrap{display:flex;align-items:center;gap:6px;flex-shrink:0}
#vol{width:70px;-webkit-appearance:none;appearance:none;height:3px;border-radius:2px;background:var(--bg3);outline:none;cursor:pointer}
#vol::-webkit-slider-thumb{-webkit-appearance:none;width:10px;height:10px;border-radius:50%;background:var(--text2);cursor:pointer}
#vol-icon{font-size:14px;color:var(--text3)}

/* ── Scrollbars ─────────────────────────────────────────────────────────── */
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
::-webkit-scrollbar-thumb:hover{background:var(--text3)}

/* ── Empty/loading states ───────────────────────────────────────────────── */
#loader{display:flex;align-items:center;justify-content:center;height:100%;flex-direction:column;gap:8px;color:var(--text3)}
#loader .spin{font-size:24px;animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
#empty{display:none;align-items:center;justify-content:center;height:100%;color:var(--text3);font-size:13px}

/* ── Responsive resizer ─────────────────────────────────────────────────── */
@media(max-width:640px){
  :root{--tree-w:0px}
  #sidebar{display:none}
  #resizer{display:none}
}
</style>
</head>
<body>
<div id="app">

  <!-- Topbar -->
  <div id="topbar">
    <span id="logo">♫ MUSIC</span>
    <input id="search" type="search" placeholder="Search tracks, albums, paths…" autocomplete="off" spellcheck="false">
    <span id="stats"></span>
    <button id="settingsbtn" title="Settings">⚙</button>
  </div>

  <!-- Settings popover -->
  <div id="settingspop">
    <h3>Settings</h3>
    <div class="sp-row"><span>Reload data from disk</span><button class="sp-btn" id="btn-reload">Reload</button></div>
    <div class="sp-row"><span>Clear IndexedDB cache</span><button class="sp-btn danger" id="btn-clear-idb">Clear cache</button></div>
    <div class="sp-row"><span>Shuffle all</span><button class="sp-btn" id="btn-shuffle-all">Shuffle</button></div>
    <div id="idb-status"></div>
  </div>

  <div id="main">
    <!-- Sidebar -->
    <div id="sidebar">
      <div id="tree-header">Library</div>
      <div id="tree"></div>
    </div>
    <div id="resizer"></div>

    <!-- Track list -->
    <div id="tracklist">
      <div id="loader"><span class="spin">◌</span><span>Loading library…</span></div>
      <div id="tracklist-header" style="display:none">
        <div></div><div>Title</div><div>Artist</div><div>Album</div><div>Time</div>
      </div>
      <div id="tracks-container"></div>
      <div id="empty">No tracks match your search.</div>
    </div>
  </div>

  <!-- Now playing bar -->
  <div id="nowbar">
    <div id="np-art"><span class="placeholder">♪</span></div>
    <div id="np-info"><div id="np-title" style="color:var(--text3)">Nothing playing</div><div id="np-artist"></div></div>
    <div id="controls">
      <button class="ctrl" id="btn-prev" title="Previous">⏮</button>
      <button class="ctrl" id="btn-play" title="Play/Pause" style="font-size:20px">▶</button>
      <button class="ctrl" id="btn-next" title="Next">⏭</button>
      <button class="ctrl" id="btn-repeat" title="Repeat">🔁</button>
      <button class="ctrl" id="btn-shuffle" title="Shuffle">🔀</button>
    </div>
    <div id="scrubber-wrap">
      <span id="time-cur">0:00</span>
      <input id="scrubber" type="range" min="0" max="100" value="0" step="0.1">
      <span id="time-tot">0:00</span>
    </div>
    <div id="vol-wrap">
      <span id="vol-icon">🔊</span>
      <input id="vol" type="range" min="0" max="1" value="0.8" step="0.01">
    </div>
  </div>
</div>

<audio id="audio" preload="metadata"></audio>

<script src="audiodata.js"></script>
<script>
// ═══════════════════════════════════════════════════════════════════════════
// MUSIC PLAYER — file:// compatible, zero dependencies
// ═══════════════════════════════════════════════════════════════════════════

const IDB_NAME    = "MusicPlayer";
const IDB_STORE   = "library";
const IDB_VER     = 1;
const IDB_KEY     = "data";
const USE_IDB_MIN = 300; // tracks threshold

// === State ===
let ALL_TRACKS    = [];
let VIEW_TRACKS   = [];   // currently displayed subset
let QUEUE         = [];   // play queue (indices into ALL_TRACKS)
let QUEUE_POS     = -1;
let ACTIVE_FOLDER = null; // null = all
let SHUFFLED      = false;
let REPEAT        = 0;    // 0=off 1=all 2=one
let SEARCHING     = false;
let idbAvailable  = false;

// === DOM refs ===
const $ = id => document.getElementById(id);
const audio       = $("audio");
const searchEl    = $("search");
const statsEl     = $("stats");
const treeEl      = $("tree");
const tracksEl    = $("tracks-container");
const loaderEl    = $("loader");
const emptyEl     = $("empty");
const headerEl    = $("tracklist-header");
const npTitle     = $("np-title");
const npArtist    = $("np-artist");
const npArt       = $("np-art");
const scrubber    = $("scrubber");
const timeCur     = $("time-cur");
const timeTot     = $("time-tot");
const volEl       = $("vol");
const idbStatus   = $("idb-status");

const fmt = s => {
  if (!s || isNaN(s)) return "0:00";
  const m = Math.floor(s/60), sec = Math.floor(s%60);
  return `${m}:${sec.toString().padStart(2,"0")}`;
};

const esc = s => s.replace(/[&<>"']/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

function highlight(str, q) {
  if (!q) return esc(str);
  const idx = str.toLowerCase().indexOf(q.toLowerCase());
  if (idx < 0) return esc(str);
  return esc(str.slice(0,idx)) +
    `<em class="hl">${esc(str.slice(idx, idx+q.length))}</em>` +
    esc(str.slice(idx+q.length));
}

// === IndexedDB ===
function openIDB() {
  return new Promise((res, rej) => {
    const req = indexedDB.open(IDB_NAME, IDB_VER);
    req.onupgradeneeded = e => e.target.result.createObjectStore(IDB_STORE);
    req.onsuccess = e => res(e.target.result);
    req.onerror   = () => rej(req.error);
  });
}
async function idbGet(db, key) {
  return new Promise((res, rej) => {
    const tx = db.transaction(IDB_STORE, "readonly");
    const req = tx.objectStore(IDB_STORE).get(key);
    req.onsuccess = () => res(req.result);
    req.onerror   = () => rej(req.error);
  });
}
async function idbSet(db, key, val) {
  return new Promise((res, rej) => {
    const tx = db.transaction(IDB_STORE, "readwrite");
    const req = tx.objectStore(IDB_STORE).put(val, key);
    req.onsuccess = () => res();
    req.onerror   = () => rej(req.error);
  });
}
async function clearIDB() {
  const db = await openIDB();
  await new Promise((res, rej) => {
    const tx = db.transaction(IDB_STORE, "readwrite");
    const req = tx.objectStore(IDB_STORE).clear();
    req.onsuccess = res; req.onerror = rej;
  });
}

// === load data ===
async function boot() {
  const raw = window.__AUDIO_DATA;
  if (!raw || !raw.tracks) {
    loaderEl.innerHTML = '<span>⚠ audiodata.js not found or empty.<br>Run scan_music.py in this folder.</span>';
    return;
  }

  const useIDB = raw.tracks.length > USE_IDB_MIN;

  if (useIDB) {
    try {
      const db = await openIDB();
      idbAvailable = true;
      const cached = await idbGet(db, IDB_KEY);
      if (cached && cached.version === raw.version) {
        // Cache hit — use IDB data
        ALL_TRACKS = cached.tracks;
        idbStatus.textContent = `IDB cache hit · v${raw.version} · ${ALL_TRACKS.length} tracks`;
      } else {
        // Cache miss — persist fresh data
        ALL_TRACKS = raw.tracks;
        await idbSet(db, IDB_KEY, { version: raw.version, tracks: raw.tracks });
        idbStatus.textContent = `IDB refreshed · v${raw.version} · ${ALL_TRACKS.length} tracks`;
      }
    } catch(e) {
      // IDB failed (e.g. file:// in some browsers) — fall through
      ALL_TRACKS = raw.tracks;
      idbStatus.textContent = "IDB unavailable — using direct load";
    }
  } else {
    ALL_TRACKS = raw.tracks;
    idbStatus.textContent = `Direct load (${ALL_TRACKS.length} tracks, IDB not needed)`;
  }

  init();
}

// === Build folder tree ======
function buildTree(tracks) {
  const root = {};
  tracks.forEach((t, i) => {
    const parts = t.folder ? t.folder.split("/") : [""];
    let node = root;
    for (const p of parts) {
      if (!node[p]) node[p] = { children: {}, indices: [] };
      node[p].indices.push(i);
      node = node[p].children;
    }
  });
  return root;
}

function renderTree(node, depth, parentPath) {
  let html = "";
  const entries = Object.entries(node).sort(([a],[b]) => a.localeCompare(b));
  for (const [name, data] of entries) {
    const path = parentPath ? `${parentPath}/${name}` : name;
    const hasChildren = Object.keys(data.children).length > 0;
    const arrow = hasChildren ? "▶" : "·";
    const cnt   = data.indices.length;
    const indent = depth * 14;
    html += `<div class="tn" data-path="${esc(path)}" data-depth="${depth}" style="padding-left:${10+indent}px" title="${esc(path)}">
      <span class="arrow">${arrow}</span>
      <span class="icon">${hasChildren ? "📁" : "📂"}</span>
      <span class="label">${esc(name || "[root]")}</span>
      <span class="cnt">${cnt}</span>
    </div>`;
    if (hasChildren) {
      html += `<div class="tc collapsed" data-parent="${esc(path)}">${renderTree(data.children, depth+1, path)}</div>`;
    }
  }
  return html;
}

// ==== track list ======
function renderTracks(tracks, query) {
  if (!tracks.length) {
    tracksEl.innerHTML = "";
    emptyEl.style.display = "flex";
    headerEl.style.display = "none";
    return;
  }
  emptyEl.style.display = "none";
  headerEl.style.display = "grid";
  const q = query || "";
  const rows = tracks.map(t => {
    const globalIdx = ALL_TRACKS.indexOf(t);
    const isPlaying = QUEUE[QUEUE_POS] === globalIdx;
    const num = t.track ? t.track : "·";
    return `<div class="tr${isPlaying?" playing":""}" data-idx="${globalIdx}">
      <div class="t-num">${num}</div>
      <div class="t-info">
        <div class="t-title">${highlight(t.title||t.path,q)}</div>
        ${t.artist?`<div class="t-sub">${highlight(t.artist,q)}</div>`:""}
      </div>
      <div class="t-artist">${highlight(t.artist||"",q)}</div>
      <div class="t-album">${highlight(t.album||"",q)}</div>
      <div class="t-dur">${fmt(t.duration)}</div>
    </div>`;
  }).join("");
  tracksEl.innerHTML = rows;
  statsEl.textContent = `${tracks.length} tracks`;
}

//====== Search =====
let searchTimer = null;
function doSearch(q) {
  q = q.trim().toLowerCase();
  SEARCHING = !!q;
  if (!q) {
    VIEW_TRACKS = ACTIVE_FOLDER
      ? ALL_TRACKS.filter(t => t.folder === ACTIVE_FOLDER || t.folder.startsWith(ACTIVE_FOLDER+"/"))
      : ALL_TRACKS;
  } else {
    const pool = ACTIVE_FOLDER
      ? ALL_TRACKS.filter(t => t.folder === ACTIVE_FOLDER || t.folder.startsWith(ACTIVE_FOLDER+"/"))
      : ALL_TRACKS;
    VIEW_TRACKS = pool.filter(t =>
      (t.title  ||"").toLowerCase().includes(q) ||
      (t.artist ||"").toLowerCase().includes(q) ||
      (t.album  ||"").toLowerCase().includes(q) ||
      (t.folder ||"").toLowerCase().includes(q)
    );
  }
  renderTracks(VIEW_TRACKS, q);
}

//////////  Playback //////////////
function buildQueue(startIdx) {
  // Queue = all VIEW_TRACKS in order, start from clicked track
  QUEUE = VIEW_TRACKS.map(t => ALL_TRACKS.indexOf(t));
  const pos = QUEUE.indexOf(startIdx);
  QUEUE_POS = pos >= 0 ? pos : 0;
  if (SHUFFLED) shuffleQueue(pos >= 0 ? pos : 0);
}

function shuffleQueue(keepFirst) {
  const first = QUEUE[keepFirst !== undefined ? keepFirst : QUEUE_POS];
  QUEUE = QUEUE.filter((_,i) => i !== (keepFirst !== undefined ? keepFirst : QUEUE_POS));
  for (let i = QUEUE.length-1; i > 0; i--) {
    const j = Math.floor(Math.random()*(i+1));
    [QUEUE[i],QUEUE[j]] = [QUEUE[j],QUEUE[i]];
  }
  QUEUE.unshift(first);
  QUEUE_POS = 0;
}

function playTrack(globalIdx) {
  const t = ALL_TRACKS[globalIdx];
  if (!t) return;
  audio.src = t.path;
  audio.volume = parseFloat(volEl.value);
  audio.play().catch(()=>{});
  updateNowBar(t);
  highlightPlaying(globalIdx);
  // save to session
  try { sessionStorage.setItem("lastTrack", globalIdx); } catch(_){}
}

function updateNowBar(t) {
  npTitle.textContent  = t.title  || t.path;
  npArtist.textContent = [t.artist, t.album].filter(Boolean).join(" — ");
  npTitle.style.color  = "";
  if (t.art) {
    npArt.innerHTML = `<img src="${t.art}" alt="">`;
  } else {
    npArt.innerHTML = `<span class="placeholder">♪</span>`;
  }
  document.title = (t.title||t.path) + " — Music";
}

function highlightPlaying(globalIdx) {
  document.querySelectorAll(".tr").forEach(el => {
    const idx = parseInt(el.dataset.idx);
    el.classList.toggle("playing", idx === globalIdx);
  });
  // scroll into view
  const el = document.querySelector(`.tr[data-idx="${globalIdx}"]`);
  if (el) el.scrollIntoView({ block:"nearest", behavior:"smooth" });
}

function playNext() {
  if (!QUEUE.length) return;
  if (REPEAT === 2) { audio.currentTime=0; audio.play(); return; }
  if (QUEUE_POS < QUEUE.length-1) {
    QUEUE_POS++;
  } else if (REPEAT === 1) {
    QUEUE_POS = 0;
  } else { return; }
  playTrack(QUEUE[QUEUE_POS]);
}

function playPrev() {
  if (audio.currentTime > 3) { audio.currentTime=0; return; }
  if (QUEUE_POS > 0) QUEUE_POS--;
  playTrack(QUEUE[QUEUE_POS]);
}

// navigation
$("btn-play").addEventListener("click", () => {
  if (audio.paused) audio.play(); else audio.pause();
});
$("btn-prev").addEventListener("click", playPrev);
$("btn-next").addEventListener("click", playNext);
$("btn-repeat").addEventListener("click", () => {
  REPEAT = (REPEAT+1)%3;
  const labels = ["🔁","🔁","🔂"];
  $("btn-repeat").textContent = labels[REPEAT];
  $("btn-repeat").classList.toggle("active", REPEAT>0);
});
$("btn-shuffle").addEventListener("click", () => {
  SHUFFLED = !SHUFFLED;
  $("btn-shuffle").classList.toggle("active", SHUFFLED);
  if (SHUFFLED && QUEUE.length) shuffleQueue();
});
$("btn-shuffle-all").addEventListener("click", () => {
  ACTIVE_FOLDER = null;
  VIEW_TRACKS = [...ALL_TRACKS];
  buildQueue(0);
  SHUFFLED = true;
  shuffleQueue();
  $("btn-shuffle").classList.add("active");
  playTrack(QUEUE[0]);
  renderTracks(VIEW_TRACKS, "");
  $("settingspop").classList.remove("open");
});

// Audio events
audio.addEventListener("timeupdate", () => {
  if (!audio.duration) return;
  scrubber.value = (audio.currentTime/audio.duration)*100;
  timeCur.textContent = fmt(audio.currentTime);
});
audio.addEventListener("loadedmetadata", () => {
  timeTot.textContent = fmt(audio.duration);
});
audio.addEventListener("ended", playNext);
audio.addEventListener("play",  () => { $("btn-play").textContent = "⏸"; });
audio.addEventListener("pause", () => { $("btn-play").textContent = "▶"; });

scrubber.addEventListener("input", () => {
  if (audio.duration) audio.currentTime = (scrubber.value/100)*audio.duration;
});
volEl.addEventListener("input", () => { audio.volume = volEl.value; });

// Keyboard shortcuts
document.addEventListener("keydown", e => {
  if (e.target.tagName === "INPUT") return;
  if (e.code === "Space")      { e.preventDefault(); audio.paused?audio.play():audio.pause(); }
  if (e.code === "ArrowRight") { audio.currentTime = Math.min(audio.duration||0, audio.currentTime+5); }
  if (e.code === "ArrowLeft")  { audio.currentTime = Math.max(0, audio.currentTime-5); }
  if (e.code === "ArrowUp")    { volEl.value=Math.min(1,+volEl.value+.05); audio.volume=volEl.value; }
  if (e.code === "ArrowDown")  { volEl.value=Math.max(0,+volEl.value-.05); audio.volume=volEl.value; }
  if (e.code === "KeyN")       playNext();
  if (e.code === "KeyP")       playPrev();
  if (e.code === "KeyF")       { searchEl.focus(); e.preventDefault(); }
});

// ── Tree interactions ─────────────────────────────────────────────────────
treeEl.addEventListener("click", e => {
  const tn = e.target.closest(".tn");
  if (!tn) return;
  const path = tn.dataset.path;

  // Toggle expand/collapse
  const childContainer = treeEl.querySelector(`.tc[data-parent="${CSS.escape(path)}"]`);
  if (childContainer) {
    const collapsed = childContainer.classList.toggle("collapsed");
    tn.classList.toggle("open", !collapsed);
  }

  // Select folder
  document.querySelectorAll(".tn").forEach(n => n.classList.remove("active"));
  tn.classList.add("active");
  ACTIVE_FOLDER = path;

  VIEW_TRACKS = ALL_TRACKS.filter(t =>
    t.folder === path || t.folder.startsWith(path+"/")
  );
  renderTracks(VIEW_TRACKS, searchEl.value.trim());
  statsEl.textContent = `${VIEW_TRACKS.length} tracks`;
});

// Show all on logo click
$("logo").style.cursor = "pointer";
$("logo").addEventListener("click", () => {
  document.querySelectorAll(".tn").forEach(n => n.classList.remove("active"));
  ACTIVE_FOLDER = null;
  VIEW_TRACKS = ALL_TRACKS;
  renderTracks(ALL_TRACKS, "");
  statsEl.textContent = `${ALL_TRACKS.length} tracks`;
});

// ── Track click → play ────────────────────────────────────────────────────
tracksEl.addEventListener("click", e => {
  const tr = e.target.closest(".tr");
  if (!tr) return;
  const idx = parseInt(tr.dataset.idx);
  buildQueue(idx);
  playTrack(idx);
});

// ── Search ────────────────────────────────────────────────────────────────
searchEl.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => doSearch(searchEl.value), 120);
});
searchEl.addEventListener("keydown", e => {
  if (e.key === "Escape") { searchEl.value = ""; doSearch(""); }
});

// ── Settings popover ──────────────────────────────────────────────────────
$("settingsbtn").addEventListener("click", e => {
  e.stopPropagation();
  $("settingspop").classList.toggle("open");
});
document.addEventListener("click", e => {
  if (!$("settingspop").contains(e.target) && e.target !== $("settingsbtn"))
    $("settingspop").classList.remove("open");
});
$("btn-clear-idb").addEventListener("click", async () => {
  await clearIDB();
  idbStatus.textContent = "IDB cleared. Reload the page to re-read audiodata.js.";
});
$("btn-reload").addEventListener("click", () => location.reload(true));

// ── Resizer ───────────────────────────────────────────────────────────────
(function() {
  const resizer = $("resizer"), sidebar = $("sidebar");
  let dragging = false, startX = 0, startW = 0;
  resizer.addEventListener("mousedown", e => {
    dragging = true; startX = e.clientX; startW = sidebar.offsetWidth;
    resizer.classList.add("dragging");
  });
  document.addEventListener("mousemove", e => {
    if (!dragging) return;
    const w = Math.max(160, Math.min(520, startW + e.clientX - startX));
    sidebar.style.width = w+"px";
  });
  document.addEventListener("mouseup", () => {
    dragging = false; resizer.classList.remove("dragging");
  });
})();

// ── Init ──────────────────────────────────────────────────────────────────
function init() {
  loaderEl.style.display = "none";

  // Sort tracks: folder → disc → track → title
  ALL_TRACKS.sort((a,b) => {
    const fc = (a.folder||"").localeCompare(b.folder||"");
    if (fc) return fc;
    if (a.disc !== b.disc) return (a.disc||0)-(b.disc||0);
    if (a.track !== b.track) return (a.track||0)-(b.track||0);
    return (a.title||"").localeCompare(b.title||"");
  });

  VIEW_TRACKS = [...ALL_TRACKS];
  statsEl.textContent = `${ALL_TRACKS.length} tracks`;

  // Build & render tree
  const TREE_DATA = buildTree(ALL_TRACKS);
  treeEl.innerHTML = renderTree(TREE_DATA, 0, "");

  // Auto-expand root if few top-level folders
  const roots = treeEl.querySelectorAll(".tn[data-depth='0']");
  if (roots.length <= 5) {
    roots.forEach(tn => {
      const path = tn.dataset.path;
      const cc = treeEl.querySelector(`.tc[data-parent="${CSS.escape(path)}"]`);
      if (cc) { cc.classList.remove("collapsed"); tn.classList.add("open"); }
    });
  }

  renderTracks(ALL_TRACKS, "");

  // Restore last track highlight (not auto-play, user must click)
  try {
    const last = sessionStorage.getItem("lastTrack");
    if (last !== null) {
      const t = ALL_TRACKS[parseInt(last)];
      if (t) { updateNowBar(t); npTitle.style.color="var(--text3)"; }
    }
  } catch(_){}
}

boot();
</script>
</body>
</html>
"""

def write_html(out_dir: Path):
    out = out_dir / HTMLFILE
    out.write_text(HTML_TEMPLATE.lstrip(), encoding="utf-8")
    print(f"✓ Wrote {HTMLFILE}")

def main():
    args = parse_args()

    # Take root from cli arg or current dir
    root = Path(args.root).resolve() if args.root else Path.cwd()
    if not root.is_dir():
        print(f"✗ Root directory not found: {root}", file=sys.stderr)
        sys.exit(1)

    out_dir = Path(args.output).resolve() if args.output else root
    out_dir.mkdir(parents=True, exist_ok=True)

    if not HAS_MUTAGEN:
        print("❌  mutagen not installed — tags will be minimal. Run: pip install mutagen")
    if not args.no_art and not HAS_PIL:
        print("❌  Pillow not installed — art will be read raw (no resize). Run: pip install Pillow")

    print(f"Root   : {root}")
    print(f"Output : {out_dir}")
    if args.exclude:
        print(f"Exclude: {', '.join(args.exclude)}")

    # Scan
    tracks = scan(root, args)
    if not tracks and not args.force_rescan:
        sys.exit(0)

    write_datafile(tracks, out_dir, root)

    # Write HTML (unless --no-html)
    if not args.no_html:
        write_html(out_dir)

    out_abs = str((out_dir / HTMLFILE).absolute())
    print(f"\nDone. Open {out_abs} in your browser.")
    webbrowser.open_new_tab(f'file://{out_abs}')

if __name__ == "__main__":
    main()
