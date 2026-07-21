#!/usr/bin/env python3
# tagman.py — An Audio(s) Manager And Downloader (CR: CL/G)
# Runs on Termux/Android, Linux, macOS, and Windows (beta/experimental) — see
# the environment-detection block below for what differs per platform.
# deps: pip install mutagen Pillow | ffmpeg (pkg/apt/brew/choco) | yt-dlp | pip install Syncedlyrics
# optional: msedit (used by Lyrics Editor if installed, otherwise falls back to nano)

import os
import re
import sys
import shutil
import threading
import time
import subprocess
import json
import unicodedata
import platform
import tempfile
from pathlib import Path

# ─── Environment Detection ──────────────────────────────────────────────────
# TagMan started as a Termux-only tool. This detects the current platform so
# behavior that's Android-specific (media preview via `am start`, the
# MediaStore rename-refresh trick, thumbnail cache location) only runs where
# it's actually needed, while Linux/macOS/Windows get sensible equivalents.

def _detect_environment():
    """Returns one of: 'termux', 'linux', 'macos', 'windows'."""
    if os.environ.get("TERMUX_VERSION") or "com.termux" in os.environ.get("PREFIX", ""):
        return "termux"
    system = platform.system()
    if system == "Windows":
        return "windows"
    if system == "Darwin":
        return "macos"
    return "linux"  # covers real Linux distros

ENV         = _detect_environment()
IS_TERMUX   = ENV == "termux"
IS_LINUX    = ENV == "linux"
IS_MACOS    = ENV == "macos"
IS_WINDOWS  = ENV == "windows"

ENV_LABEL = {
    "termux":  "Termux Audio Tool",
    "linux":   "Linux Audio Tool",
    "macos":   "macOS Audio Tool (beta)",
    "windows": "Windows Audio Tool (beta/experimental)",
}[ENV]

def _thumb_cache_dir():
    """Where temp preview thumbnails get written. Termux uses the shared
    Download folder so Android's intent system can hand it to a viewer app;
    everywhere else uses a private tagman folder under the OS temp dir."""
    if IS_TERMUX:
        d = Path("/sdcard/Download")
    else:
        d = Path(tempfile.gettempdir()) / "tagman"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        d = Path.cwd()
    return d

def _open_image_preview(path):
    """Open an image file for the user to look at, picking the right
    mechanism per platform. Returns True if a viewer was launched."""
    try:
        if IS_TERMUX:
            subprocess.Popen([
                "am", "start", "-a", "android.intent.action.VIEW",
                "-t", "image/jpeg", "-d", f"file://{path}"
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif IS_MACOS:
            subprocess.Popen(["open", str(path)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif IS_WINDOWS:
            # Experimental: relies on os.startfile launching the default
            # image viewer. Untested across all Windows configurations.
            os.startfile(str(path))  # nosec - local file we just wrote
        else:  # real Linux/distro
            subprocess.Popen(["xdg-open", str(path)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        print(f"  {Y}[!]{R} Could not open image preview: {e}")
        return False

# ─── Config ───────────────────────────────────────────────────────────────────
# Looks for ".tagman_config.json" next to tagman.py itself -- NOT the current
# working directory. This has to be Path(__file__).resolve().parent rather
# than Path.cwd(): TagMan is routinely launched from other folders (a launcher
# symlink/wrapper sitting inside whatever music folder you're managing, per
# the Folder Shortcut feature), and Path.cwd() there would silently point at
# a fresh, folder-local config instead of the one real config that lives with
# the script in $HOME -- so every symlinked folder used to get its own blank
# settings instead of sharing the one config. .resolve() follows the symlink
# back to tagman.py's real location so this lands in the same place ($HOME,
# normally) no matter which folder -- or which symlink into it -- was used to
# launch TagMan.
try:
    CONFIG_PATH = Path(__file__).resolve().parent / ".tagman_config.json"
except Exception:
    CONFIG_PATH = Path.cwd() / ".tagman_config.json"

DEFAULT_CONFIG = {
    "remember_format":  None,        # None (always ask) / "m4a" / "mp3" / "flac" / "opus"
    "remember_quality": None,        # None (always ask) / "best" (128kbps default) / "160" / "192" / "256" / "320" / "968" (not used for flac)
    "preview_threshold": 7,          # how many thumbnails to preview before skipping the rest
    "rename_mode": "ask",            # "ask" (always prompt) / "auto" (do silently) / "off" (do nothing)
    "video_search_mode": "ask",      # "ask" (prompt each search) / "always" (always search videos too) / "never" (songs-only, old behavior)
    "folder_reset_mode": "ignore",   # "always_ask" (cd back to $HOME once the task using the picked folder is done, every environment) / "ignore" (stay in that folder until the user exits, old behavior)
}

def _load_config():
    """Read .tagman_config.json from next to tagman.py (CONFIG_PATH) if
    present and merge it over DEFAULT_CONFIG. Missing file, missing fields,
    or broken JSON all silently fall back to DEFAULT_CONFIG without crashing
    TagMan."""
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k in DEFAULT_CONFIG:
                    if k in data:
                        cfg[k] = data[k]
        except Exception:
            pass
    return cfg

def _save_config(cfg):
    """Write config to CONFIG_PATH with clean JSON formatting."""
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        return True
    except Exception:
        return False

CONFIG = _load_config()

# ─── History Log ────────────────────────────────────────────────────────────
# A running log of what TagMan has done in each folder -- tag edits, lyrics
# embeds, cover embeds, and downloads (with the source URL) -- each entry
# stamped with a local day-month-year date, a HH:MM:SS time, and which
# folder it happened in. Stored centrally next to tagman.py (same place as
# the config, for the same symlink/launcher reason -- see CONFIG_PATH above)
# so Settings -> History can be opened from anywhere, but every read filters
# down to just the entries whose "folder" matches the CURRENT working
# directory, so one folder's history never bleeds into another's.
try:
    HISTORY_PATH = Path(__file__).resolve().parent / ".tagman_history.json"
except Exception:
    HISTORY_PATH = Path.cwd() / ".tagman_history.json"

_HISTORY_MAX_ENTRIES = 1000  # oldest entries (across all folders) drop off past this

def _history_load_all():
    if not HISTORY_PATH.exists():
        return []
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []

def _history_save_all(entries):
    try:
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2, ensure_ascii=False)
        return True
    except Exception:
        return False

def _history_log(entry_type, action, file=None, url=None):
    """Append one history entry, scoped to the current folder. Never raises
    -- a logging failure should never break the edit/download it's trying
    to record."""
    try:
        now = time.localtime()
        entry = {
            "type": entry_type,      # "edit" / "lyrics" / "cover" / "download"
            "action": action,        # short human-readable description
            "file": file,
            "url": url,
            "folder": str(Path.cwd().resolve()),
            "date": time.strftime("%d-%m-%Y", now),  # local day-month-year
            "time": time.strftime("%H:%M:%S", now),
            "timestamp": time.strftime("%d-%m-%Y %H:%M:%S", now),
        }
        entries = _history_load_all()
        entries.append(entry)
        if len(entries) > _HISTORY_MAX_ENTRIES:
            entries = entries[-_HISTORY_MAX_ENTRIES:]
        _history_save_all(entries)
    except Exception:
        pass

def _history_for_this_folder():
    """Every logged entry whose 'folder' matches cwd, oldest-first as
    stored (callers decide display order)."""
    try:
        here = str(Path.cwd().resolve())
    except Exception:
        here = str(Path.cwd())
    return [e for e in _history_load_all() if e.get("folder") == here]

# ─── ANSI colors ──────────────────────────────────────────────────────────────
R  = "\033[0m"         # reset
V  = "\033[38;5;135m"  # violet (dominant)
LV = "\033[38;5;183m"  # light violet
P  = "\033[38;5;141m"  # purple accent
C  = "\033[38;5;117m"  # cyan accent
G  = "\033[38;5;120m"  # green (success)
Y  = "\033[38;5;228m"  # yellow (warning/info)
DIM= "\033[38;5;245m"  # dim gray

def _ts():
    """Current wall-clock time as a dim ANSI-colored '[HH:MM:SS]' stamp, used
    to mark when a process/batch operation begins and ends."""
    return f"{DIM}[{time.strftime('%H:%M:%S')}]{R}"

# ─── ASCII Art ──────────────────────────────────────────────────────────────
ASCII_BANNER = f"""
{V}   ████████╗ █████╗  ██████╗ ███╗   ███╗ █████╗ ███╗   ██╗
   ╚══██╔══╝██╔══██╗██╔════╝ ████╗ ████║██╔══██╗████╗  ██║
      ██║   ███████║██║  ███╗██╔████╔██║███████║██╔██╗ ██║
      ██║   ██╔══██║██║   ██║██║╚██╔╝██║██╔══██║██║╚██╗██║
      ██║   ██║  ██║╚██████╔╝██║ ╚═╝ ██║██║  ██║██║ ╚████║
      ╚═╝   ╚═╝  ╚═╝ ╚═════╝ ╚═╝     ╚═╝╚═╝  ╚═╝╚═╝  ╚═══╝{R}
"""

EXIT_BANNER = f"""
{V}   ╔══════════════════════════════════════════╗
{V}   ║{'(≧▽≦)':^42}║
{V}   ║{'(⁄ω⁄)':^42}║
{V}   ╚══════════════════════════════════════════╝{R}
"""

# Small violet banner shown before a longer download/lyrics-fetch operation
# starts, so waiting on yt-dlp/lyrics providers doesn't feel like dead air.
PATIENCE_BANNER = f"""
{V}   ╔══════════════════════════════════════════╗
{V}   ║{'♫  ~  ♫':^42}║
{V}   ║{LV}{'Good things come to those who wait.':^42}{V}║
{V}   ╚══════════════════════════════════════════╝{R}
"""

def _print_patience_banner():
    print(PATIENCE_BANNER)

# ─── shared UI helper ─────────────────────────────────────────────────────────

MAIN_MENU_CODE = "324"  # type this at any number/search prompt to return to main menu
THUMB_CACHE_PATH = str(_thumb_cache_dir() / "tagman_thumb.jpg")  # temporary location for thumbnail preview

class _ToMainMenu(BaseException):
    """Raised when user types 324 at any prompt. Deliberately inherits from
    BaseException so it won't be caught accidentally by except Exception blocks."""
    pass

def _ask(prompt):
    """Replacement for input() for number/search/URL prompts.
    - Empty -> re-prompt
    - 324   -> raise _ToMainMenu, returns to main menu from anywhere.
    - Otherwise -> returned as-is (unstripped)."""
    while True:
        raw = input(prompt)
        stripped = raw.strip()
        if stripped == MAIN_MENU_CODE:
            raise _ToMainMenu()
        if stripped == "":
            continue
        return raw

def _ask_optional(prompt):
    """Like _ask(), but for prompts where an empty Enter is a valid answer
    (e.g. 'Enter to skip'). _ask() re-prompts forever on empty input, which
    is right for menu/number prompts but wrong here -- this variant returns
    "" immediately instead of looping. Still honors the main-menu shortcut."""
    raw = input(prompt)
    if raw.strip() == MAIN_MENU_CODE:
        raise _ToMainMenu()
    return raw

def _vlen(s):
    """Visual length: Wide/Fullwidth characters count as 2 columns."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in s)

def _term_width():
    """Visible terminal columns, with a safe fallback for pipes/non-ttys.
    Boxes cap their width to this so long rows don't wrap around and leave
    the trailing border character stranded on its own line (the extra blank
    '║' rows seen on narrow phone terminals)."""
    try:
        import shutil
        return shutil.get_terminal_size(fallback=(56, 24)).columns
    except Exception:
        return 56

def _sub_box(title, items, width=50, icon="✦"):
    """Fully-enclosed titled sub-menu box (violet palette).
    items: list of (key, label, desc) tuples — desc may be None/"" for a bare line."""
    cap = max(30, _term_width() - 4)
    width = min(width, cap)
    head = f"═ {icon} {title} "
    fill = max(0, width - _vlen(head))
    print(f"\n{V}  ╔{head}{'═' * fill}╗{R}")
    for key, label, desc in items:
        col = DIM if str(key) == "0" else LV
        label = _fit_name(label, maxlen=max(8, width - 6))
        plain1 = f"  {key}. {label}"
        pad1 = max(0, width - _vlen(plain1))
        print(f"{V}  ║{R}  {P}{key}{V}.{R} {col}{label}{R}{' ' * pad1}{V}║{R}")
        if desc:
            desc = _fit_name(desc, maxlen=max(8, width - 5))
            plain2 = f"     {desc}"
            pad2 = max(0, width - _vlen(plain2))
            print(f"{V}  ║{R}     {DIM}{desc}{R}{' ' * pad2}{V}║{R}")
    print(f"{V}  ╚{'═' * width}╝{R}")

def _box(title, rows, min_width=44, max_width=72, icon="✦"):
    """Fully-enclosed dynamic-width box for variable/list content.
    rows: list of (plain, colored) tuples — plain is the ANSI-free text used only to measure
    width & padding, colored is what actually gets printed."""
    cap = max(30, _term_width() - 4)
    max_width = min(max_width, cap)
    min_width = min(min_width, max_width)
    content_max = max([_vlen(p) for p, _ in rows] + [_vlen(title) + 4], default=_vlen(title) + 4)
    width = min(max(min_width, content_max), max_width)
    head = f"═ {icon} {title} "
    fill = max(0, width - _vlen(head))
    print(f"\n{V}  ╔{head}{'═' * fill}╗{R}")
    for plain, colored in rows:
        pad = max(0, width - _vlen(plain))
        print(f"{V}  ║{R}{colored}{' ' * pad}{V}║{R}")
    print(f"{V}  ╚{'═' * width}╝{R}")

# ─── stay ───────────────────────────────────────────────────────────────

_BIGFONT_5x5 = {
    'T': ["11111", "..1..", "..1..", "..1..", "..1.."],
    'A': [".111.", "1...1", "11111", "1...1", "1...1"],
    'G': [".1111", "1....", "1.111", "1...1", ".1111"],
    'M': ["1...1", "11.11", "1.1.1", "1...1", "1...1"],
    'N': ["1...1", "11..1", "1.1.1", "1..11", "1...1"],
    'S': [".1111", "1....", ".111.", "....1", "1111."],
    "'": ["11", "11", "..", "..", ".."],
    ' ': ["...", "...", "...", "...", "..."],
}

def _big_text(text, fill="█"):
    """Render text as big 5-row block-letter ASCII using a tiny built-in bitmap font."""
    rows = ["" for _ in range(5)]
    for ch in text:
        glyph = _BIGFONT_5x5.get(ch.upper(), _BIGFONT_5x5[' '])
        for r in range(5):
            rows[r] += glyph[r].replace('1', fill).replace('.', ' ') + ' '
    return rows

def _wait_or_enter(seconds, prompt):
    """Wait for ENTER or timeout `seconds` seconds, whichever comes first."""
    print(prompt)
    try:
        import select
        ready, _, _ = select.select([sys.stdin], [], [], seconds)
        if ready:
            sys.stdin.readline()
    except Exception:
        time.sleep(seconds)

def _stay():
    os.system("clear")
    bar = "✦ ° . ⋆ ☆ . ° ✦ ° . ⋆ ☆ . ° ✦ ° . ⋆ ☆ . ° ✦"
    print(f"\n{C}  {bar}{R}\n")
    for row in _big_text("TAGMAN'S"):
        print(f"{V}   {row}{R}")
    print(f"\n{DIM}{'Sneeky, huh? :)':^49}{R}\n")

    credit_rows = [
        (f"  ⚡ Gya", f"  {P}⚡{R} {LV}Gya{R}"),
        (f"      idea, design, & research documentation (brain)", f"      {DIM}idea, design, & research documentation (brain){R}"),
        (f"  ✎ Claude & DeepSeek", f"  {P}✎{R} {LV}Claude{R} {DIM}&{R} {LV}DeepSeek{R}"),
        (f"      code implementation", f"      {DIM}code implementation{R}"),
        (f"  ⚙ deniscerri", f"  {P}⚙{R} {LV}deniscerri{R}"),
        (f"      for his yt-dlnis", f"      {DIM}for his yt-dlnis{R}"),
        (f"  ♪ yt-dlp", f"  {P}♪{R} {LV}yt-dlp{R}"),
        (f"      without this, TagMan wouldn't exist", f"      {DIM}without this, TagMan wouldn't exist{R}"),
        (f"  ✧ & others", f"  {P}✧{R} {LV}& others{R}"),
        (f"      all open-source libraries TagMan uses", f"      {DIM}all open-source libraries TagMan uses{R}"),
    ]
    _box("Hall of Fame", credit_rows, min_width=54, icon="♡")
    print(f"\n{C}  {bar}{R}")

    _wait_or_enter(7, f"\n{DIM}  Press ENTER to continue, or wait 7 seconds...{R}")
    os.system("clear")

def _leave_lyric_mark(fp):
    """Re-run the TN-marker + metadata-header cleanup on a file's EXISTING
    lyrics (via _embed_lyrics, the single funnel for all lyrics writes),
    without needing to fetch new lyrics or open the nano editor. Useful to
    retro-fix files that were embedded by an older/broken version. Returns
    True if the file had lyrics and got reprocessed, False if it had none."""
    lyrics = _get_lyrics(fp)
    if not lyrics:
        return False
    _embed_lyrics(fp, lyrics)
    return True

def _secret_comment_menu():
    """Secret menu (trigger: type 33333 at 'Pick menu'). Every category here
    (Comment / Copyright / Leave Mark on Lyric) shares the same Single/Batch
    shape: Single lets you pick one-or-more files by number (pick_files()),
    Batch runs on every audio file in the folder."""
    os.system("clear")
    print(f"\n{V}  ✦ ° . ⋆ ☆ . ° ✦ ° . ⋆ ☆ . ° ✦{R}\n")
    print(f"{DIM}{'Sneeky again, huh? :)':^49}{R}\n")

    _sub_box("Secret Menu", [
        (1, "Comment",             "write a custom comment (single/batch)"),
        (2, "Copyright",           "set copyright tag (single/batch)"),
        (3, "Leave Mark on Lyric", "re-stamp TN marker (single/batch)"),
        (0, "Back",                None),
    ], width=52, icon="✎")

    try:
        sub = int(_ask(f"\n{V}  ❯{R} Pick: "))
    except ValueError:
        print(f"  {Y}[!]{R} Invalid choice.")
        return
    if sub == 0:
        return
    if sub not in (1, 2, 3):
        print(f"  {Y}[!]{R} Invalid choice.")
        return

    label = {1: "Comment", 2: "Copyright", 3: "Leave Mark on Lyric"}[sub]
    _sub_box(label, [
        (1, "Single", "select file(s) by number (multi allowed)"),
        (2, "Batch",  "every audio file in this folder"),
        (0, "Back",    None),
    ], width=52, icon="✎")
    try:
        csub = int(_ask(f"\n{V}  ❯{R} Pick: "))
    except ValueError:
        return
    if csub == 0:
        return
    if csub not in (1, 2):
        print(f"  {Y}[!]{R} Invalid choice.")
        return

    if csub == 1:
        fps = pick_files()
        if not fps:
            return
    else:
        fps = scan_audio()
        if not fps:
            print(f"\n{Y}[!]{R} {_scan_empty_message('No audio files in this folder.')}")
            return

    if sub == 1:  # Comment
        comment = _ask(f"\n  {C}Comment text{R}: ").strip()
        if not comment:
            print(f"  {DIM}Cancelled (empty comment).{R}")
            return
        for fp in fps:
            _stamp_comment(fp, comment, force=True)
        print(f"\n  {G}[+]{R} Comment written to {len(fps)} file(s).")

    elif sub == 2:  # Copyright
        value = _ask(f"\n  {C}Copyright text{R}: ").strip()
        if not value:
            print("Cancelled.")
            return
        for fp in fps:
            _set_tag_single(fp, "copyright", value)
            _stamp_comment(fp, "Edited & Embedded by TagMan")
        print(f"\n  {G}[+]{R} Copyright set on {len(fps)} file(s).")

    else:  # Leave Mark on Lyric
        marked, skipped = 0, []
        for fp in fps:
            if _leave_lyric_mark(fp):
                marked += 1
            else:
                skipped.append(fp.name)
        print(f"\n  {G}[+]{R} Lyric mark refreshed on {marked} file(s).")
        if skipped:
            print(f"  {DIM}[—] No lyrics, skipped: {', '.join(skipped)}{R}")


# ─── file picker ──────────────────────────────────────────────────────────────

_SCAN_CANCELLED = False  # set by _maybe_pick_scan_folder(); read by _scan_empty_message()
_SCAN_DID_CD     = False  # set by _maybe_pick_scan_folder(); read by _apply_folder_reset_policy()

def _maybe_pick_scan_folder():
    """Every time a feature needs to scan for audio files, if TagMan is
    sitting with $HOME itself as the working directory it means it wasn't
    cd'd into a specific music folder first — e.g. it was launched straight
    from $HOME rather than via a folder-shortcut wrapper (whose whole point
    is running with the target folder already as cwd). In that case, offer
    the same interactive folder browser used by Sorcerer/Settings -> Folder
    Shortcut, and `cd` into whatever gets picked, so every feature that
    scans for songs (cover art, tags, lyrics, metadata export/import) works
    on that folder.

    This is "always ask", not "ask once": it re-prompts on every scan
    attempt as long as cwd is still $HOME. Once a folder gets picked,
    os.chdir() moves cwd out of $HOME so subsequent scans no longer trigger
    this at all — but if the picker gets cancelled (q / Ctrl+C / Ctrl+D),
    cwd stays $HOME on purpose, so the very next scan attempt asks again
    instead of silently locking the user out of picking a folder.

    Only triggers when cwd is genuinely $HOME — a shortcut sitting
    elsewhere (including a symlink-style wrapper in some other folder)
    already has the right cwd and this stays out of the way. Windows is
    included for consistency but isn't the focus; the same Path.home()
    check applies there too."""
    global _SCAN_CANCELLED
    _SCAN_CANCELLED = False

    try:
        cwd_is_home = Path.cwd().resolve() == Path.home().resolve()
    except Exception:
        cwd_is_home = False
    if not cwd_is_home:
        return

    print(f"\n  {LV}[i]{R} Pick a folder to scan for music in ({P}q{R} to use the current folder).")
    if IS_TERMUX and (Path.home() / "storage" / "shared").exists():
        start = Path.home() / "storage" / "shared"
    else:
        start = Path.home()

    target = _pick_folder_interactive(start)
    os.system("clear")
    if target is None:
        _SCAN_CANCELLED = True
        return
    global _SCAN_DID_CD
    try:
        os.chdir(target)
        _SCAN_DID_CD = True
    except Exception as e:
        print(f"  {Y}[!]{R} Could not switch to that folder: {e}")

def _scan_empty_message(default="No .mp3/.m4a/.flac/.opus files in this folder."):
    """Message to show when scan_audio() came back empty. Distinguishes a
    genuinely empty/wrong folder from the user cancelling the folder-picker
    (q / Ctrl+C / Ctrl+D) that scan_audio() may have triggered via
    _maybe_pick_scan_folder(), so cancelling doesn't get misreported as
    'no files here'."""
    return "Cancelled by user." if _SCAN_CANCELLED else default

def _apply_folder_reset_policy():
    """Runs once per main-menu loop iteration (i.e. right after whatever
    action the user picked has fully finished). Applies the
    'folder_reset_mode' Settings choice:
      - "always_ask": cd back to $HOME now that the task is done, on every
        environment (Termux/Linux/macOS/Windows) -- regardless of whether
        *this particular* action was the one that cd'd away. Folders can
        also get picked outside of _maybe_pick_scan_folder() (Sorcerer's
        destination picker, folder shortcuts, etc.), so gating the reset
        behind "did this exact action just cd" left cwd stuck away from
        $HOME once any of those ran, which meant the next feature never
        re-triggered the picker and silently reused a stale folder. Always
        checking cwd here instead means every feature that follows a
        finished action re-asks, exactly as "always ask" implies.
      - "ignore" (default): stay put, exactly like TagMan always has --
        the picked folder remains cwd until the user exits."""
    global _SCAN_DID_CD
    _SCAN_DID_CD = False
    if CONFIG.get("folder_reset_mode", "ignore") != "always_ask":
        return
    try:
        if Path.cwd().resolve() == Path.home().resolve():
            return
        os.chdir(Path.home())
    except Exception as e:
        print(f"  {Y}[!]{R} Could not return to $HOME: {e}")

def scan_audio():
    _maybe_pick_scan_folder()
    files = []
    for ext in ("*.mp3", "*.m4a", "*.flac", "*.opus"):
        files.extend(Path(".").glob(ext))
    return sorted(files)

def _fit_name(name, maxlen=48):
    """Truncate a long file name so a single list row can't overflow the box
    width (long names used to push the row past _box()'s right border,
    breaking alignment on narrower phone terminals)."""
    return name if len(name) <= maxlen else name[:maxlen - 1].rstrip() + "…"

def _sorted_song_picks(audio_files):
    """Pair each audio file with an 'Artist - Title' display label (falling
    back to just the title, or the filename stem, when tags are missing),
    and sort A-Z by that label. This is the single shared ordering/format
    used by every song-picker list in TagMan (Fetch Thumbnail, Redownload,
    Batch Lyrics manual pick, ...) so the numbering is always predictable
    instead of following whatever order files happened to scan in."""
    items = []
    for afp in audio_files:
        t, a = _get_tags(afp)
        title = t or afp.stem
        label = f"{a} - {title}" if a else title
        items.append((afp, title, a, label))
    items.sort(key=lambda x: x[3].lower())
    return items

def _song_pick_rows(items, maxlen=50):
    """Build _box()-ready rows (plain, colored) from _sorted_song_picks()
    output, numbered 1..N to match the sorted order."""
    rows = []
    for i, (afp, title, a, label) in enumerate(items, 1):
        label_f = _fit_name(label, maxlen=maxlen)
        rows.append((f"  {i:>2}. {label_f}", f"  {P}{i:>2}{R}. {LV}{label_f}{R}"))
    return rows

def pick_file(show_album=False):
    """Single file picker (used only where exactly one file is needed)."""
    files = scan_audio()
    if not files:
        print(f"\n{Y}[!]{R} {_scan_empty_message()}")
        return None
    rows = []
    for i, f in enumerate(files, 1):
        a, aa, alb = _get_artist_album(f)
        name = _fit_name(f.name)
        rows.append((f"  {i:>2}. {name}", f"  {P}{i:>2}{R}. {LV}{name}{R}"))
        if show_album:
            alb_str = alb if alb else "No Album"
            rows.append((f"      Album: {alb_str}", f"      {DIM}Album: {alb_str}{R}"))
        else:
            rows.append((f"      A={a}  AA={aa}", f"      {DIM}A={a}  AA={aa}{R}"))
    rows.append((f"   0. Back", f"   {P}0{R}. {DIM}Back{R}"))
    _box("Pick File", rows, min_width=50, icon="☰")
    try:
        c = int(_ask(f"\n{V}  ❯{R} Number: "))
        if c == 0:
            return None
        if 1 <= c <= len(files):
            return files[c - 1]
    except ValueError:
        pass
    print(f"{Y}  [!]{R} Invalid choice.")
    return None

def pick_files(show_album=False):
    """Multi file picker (used in all "single" modes to allow multiple selections)."""
    files = scan_audio()
    if not files:
        print(f"\n{Y}[!]{R} {_scan_empty_message()}")
        return []
    rows = []
    for i, f in enumerate(files, 1):
        a, aa, alb = _get_artist_album(f)
        name = _fit_name(f.name)
        rows.append((f"  {i:>2}. {name}", f"  {P}{i:>2}{R}. {LV}{name}{R}"))
        if show_album:
            alb_str = alb if alb else "No Album"
            rows.append((f"      Album: {alb_str}", f"      {DIM}Album: {alb_str}{R}"))
        else:
            rows.append((f"      A={a}  AA={aa}", f"      {DIM}A={a}  AA={aa}{R}"))
    rows.append((f"   0. Back", f"   {P}0{R}. {DIM}Back{R}"))
    _box("Pick File(s)", rows, min_width=50, icon="☰")
    raw = _ask(f"\n{V}  ❯{R} Number(s) (e.g., 1,3,5): ").strip()
    if raw == "0" or not raw:
        return []
    picks = _parse_multi_input(raw, len(files))
    if not picks:
        print(f"{Y}  [!]{R} No valid numbers.")
        return []
    return [files[i-1] for i in picks]


# ─── features ─────────────────────────────────────────────────────────────────

def extract_cover(fp):
    out = Path(fp).stem + "_cover.jpg"
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", str(fp), "-an", "-vcodec", "copy", out],
        capture_output=True, text=True
    )
    if Path(out).exists():
        print(f"\n{G}[+]{R} Cover saved: {out}")
        try:
            from PIL import Image
            img = Image.open(out)
            print(f"    Size: {img.size[0]}x{img.size[1]}px")
        except ImportError:
            print("    (install Pillow to see dimensions: pip install Pillow)")
    else:
        print(f"{Y}[!]{R} Failed — maybe the file has no cover art.")


def _open_vorbis(fp, ext):
    """Open a FLAC or Ogg Opus file via mutagen's shared Vorbis-comment-style
    dict interface -- both support audio['key'] = [...] and audio.save() the
    same way, so the many tag-only call sites below can treat the two
    formats identically instead of duplicating each block. Cover art is the
    one thing that differs (see _opus_pictures/_opus_set_picture/
    _opus_clear_pictures below) since OggOpus has no built-in .pictures /
    .add_picture() like FLAC does."""
    if ext == ".flac":
        from mutagen.flac import FLAC
        return FLAC(str(fp))
    from mutagen.oggopus import OggOpus
    return OggOpus(str(fp))

def _opus_pictures(audio):
    """Return embedded cover art as mutagen.flac.Picture objects for an
    OggOpus file. Ogg-based formats (Vorbis/Opus/Speex) don't have a
    dedicated picture block like FLAC -- the Xiph-standard way to embed one
    is a base64-encoded FLAC Picture block stashed under the
    'metadata_block_picture' vorbis-comment key, so we reuse mutagen's FLAC
    Picture (de)serializer for it."""
    from mutagen.flac import Picture
    import base64
    pics = []
    for b64 in audio.get("metadata_block_picture", []):
        try:
            pics.append(Picture(base64.b64decode(b64)))
        except Exception:
            pass
    return pics

def _opus_set_picture(audio, pic):
    """Replace all embedded cover art in an OggOpus file with a single
    Picture (mirrors FLAC's clear_pictures() + add_picture() pair)."""
    import base64
    audio["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]

def _opus_clear_pictures(audio):
    audio["metadata_block_picture"] = []

def _embed_cover(fp, cover_path):
    """Embed cover image into audio file.

    Also removes any PRE-EXISTING cover art before adding the new one. This
    matters because ID3 keys an APIC frame by its description text (HashKey =
    'APIC:' + desc) — if a file already has one embedded under a different
    desc (e.g. yt-dlp's own thumbnail step tags it 'attached picture' while
    we've always used desc='Cover'), mutagen's .add() does NOT overwrite it;
    it just appends a second, separate APIC frame. Most players/file managers
    then display whichever APIC comes FIRST in the file — usually the old
    one — so the new cover silently never shows even though it's genuinely
    embedded. Also strips leftover WXXX 'custom URL' frames (e.g. yt-dlp's
    Front Cover URL / Referrer URL), which are just stale clutter once a real
    image is embedded here.
    """
    ext = Path(fp).suffix.lower()
    mime = "image/png" if str(cover_path).lower().endswith(".png") else "image/jpeg"
    if ext == ".m4a":
        from mutagen.mp4 import MP4, MP4Cover
        fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
        audio = MP4(str(fp))
        with open(cover_path, "rb") as f:
            audio["covr"] = [MP4Cover(f.read(), imageformat=fmt)]  # single key: always fully replaces
        audio.save()
    elif ext == ".flac":
        from mutagen.flac import FLAC, Picture
        audio = FLAC(str(fp))
        audio.clear_pictures()  # single call: always fully replaces, same intent as the m4a branch above
        pic = Picture()
        pic.type = 3  # "Cover (front)"
        pic.mime = mime
        pic.desc = "Cover"
        with open(cover_path, "rb") as f:
            pic.data = f.read()
        audio.add_picture(pic)
        audio.save()
    elif ext == ".opus":
        from mutagen.flac import Picture
        from mutagen.oggopus import OggOpus
        audio = OggOpus(str(fp))
        _opus_clear_pictures(audio)  # same "always fully replaces" intent as m4a/flac above
        pic = Picture()
        pic.type = 3  # "Cover (front)"
        pic.mime = mime
        pic.desc = "Cover"
        with open(cover_path, "rb") as f:
            pic.data = f.read()
        _opus_set_picture(audio, pic)
        audio.save()
    else:
        from mutagen.mp3 import MP3
        from mutagen.id3 import ID3, APIC, error as ID3Error
        try:
            audio = MP3(str(fp), ID3=ID3)
            if audio.tags is None:
                audio.add_tags()
        except ID3Error:
            audio.add_tags()
        # Remove ALL existing cover art (any desc) and stale "custom URL"
        # frames before adding the new one, so exactly one cover survives.
        for key in list(audio.tags.keys()):
            if key.startswith("APIC") or key.startswith("WXXX"):
                del audio.tags[key]
        with open(cover_path, "rb") as f:
            audio.tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=f.read()))
        audio.save()
    _history_log("cover", "Cover art embedded", file=Path(fp).name)

def _has_cover_art(fp):
    """True if fp already has an actual embedded cover image (APIC/covr).
    Needed because yt-dlp's --embed-thumbnail postprocessor can silently fail
    for some MP3s (ffmpeg/thumbnail-conversion edge cases) while
    --embed-metadata still succeeds — leaving only text metadata that
    *references* the cover URL (WXXX frames) with no actual image data, which
    looks fine in a tag dump but never shows a thumbnail anywhere."""
    ext = Path(fp).suffix.lower()
    try:
        if ext == ".m4a":
            from mutagen.mp4 import MP4
            audio = MP4(str(fp))
            return bool(audio.tags and audio.tags.get("covr"))
        elif ext == ".flac":
            from mutagen.flac import FLAC
            audio = FLAC(str(fp))
            return bool(audio.pictures)
        elif ext == ".opus":
            from mutagen.oggopus import OggOpus
            audio = OggOpus(str(fp))
            return bool(_opus_pictures(audio))
        else:
            from mutagen.id3 import ID3
            tags = ID3(str(fp))
            return any(k.startswith("APIC") for k in tags.keys())
    except Exception:
        return False


def _crop_square(img_path, size=720):
    """Crop center square and resize to size x size."""
    try:
        from PIL import Image
        img  = Image.open(str(img_path))
        w, h = img.size
        side = min(w, h)
        img  = img.crop(((w-side)//2, (h-side)//2, (w+side)//2, (h+side)//2))
        img  = img.resize((size, size), Image.LANCZOS)
        img.save(str(img_path), quality=95)
        return True
    except Exception as e:
        print(f"  {Y}[!]{R} Crop failed: {e}")
        return False

def _tokenize(name):
    import re
    words = re.split(r'[^\w]+', name.lower())
    return {w for w in words if len(w) > 2}

def _fuzzy_word_match(a, b):
    """True if two words are similar: one is prefix of the other, or edit distance ≤ 1."""
    if a == b:
        return True
    if a.startswith(b) or b.startswith(a):
        return True
    if abs(len(a) - len(b)) <= 2:
        shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
        if longer.startswith(shorter[:max(2, len(shorter)-1)]):
            return True
    return False

def _fuzzy_token_score(tokens_a, tokens_b):
    """Count how many tokens in tokens_a have a fuzzy match in tokens_b."""
    score = 0
    for wa in tokens_a:
        for wb in tokens_b:
            if _fuzzy_word_match(wa, wb):
                score += 1
                break
    return score

def _match_img_to_audio(img, audio_files):
    """Try to match an image to a song. Return best (afp, score) or (None, 0)."""
    img_tokens = _tokenize(img.stem)
    if not img_tokens:
        return None, 0.0
    best, best_score = None, 0.0
    for afp in audio_files:
        title, _ = _get_tags(afp)
        if title:
            audio_tokens = _tokenize(title)
            if audio_tokens:
                s1 = _fuzzy_token_score(img_tokens, audio_tokens) / len(img_tokens)
                s2 = _fuzzy_token_score(audio_tokens, img_tokens) / len(audio_tokens)
                score = (2 * s1 * s2 / (s1 + s2)) if (s1 + s2) > 0 else 0
                if score > best_score:
                    best_score, best = score, afp
        file_tokens = _tokenize(afp.stem)
        if file_tokens:
            s1 = _fuzzy_token_score(img_tokens, file_tokens) / len(img_tokens)
            s2 = _fuzzy_token_score(file_tokens, img_tokens) / len(file_tokens)
            score = (2 * s1 * s2 / (s1 + s2)) if (s1 + s2) > 0 else 0
            if score > best_score:
                best_score, best = score, afp
    return best, best_score

def insert_cover(fp=None, _manual=True):
    if _manual:
        img_files = []
        for iext in ("*.jpg", "*.jpeg", "*.png", "*.JPG", "*.JPEG", "*.PNG"):
            img_files.extend(sorted(Path(".").glob(iext)))
        if not img_files:
            print(f"\n{Y}[!]{R} No image files in this folder.")
            return

        print(f"\n{V}  ╭═ Pick Image ══════════════════════════════╮{R}")
        for i, img in enumerate(img_files, 1):
            size_kb = img.stat().st_size // 1024
            print(f"{V}  │{R}  {P}{i:>2}{R}. {LV}{img.name}{R}  {DIM}({size_kb} KB){R}")
        print(f"{V}  │{R}   {P}0{R}. {DIM}Back{R}")
        print(f"{V}  ╰{'─' * 46}╯{R}")

        raw = _ask(f"\n{V}  ❯{R} Number(s) (multi allowed, e.g., 1,3,4): ").strip()
        if raw == "0" or not raw:
            return
        picks = _parse_multi_input(raw, len(img_files))
        if not picks:
            print(f"{Y}[!]{R} Invalid number(s).")
            return

        selected_imgs = [img_files[i-1] for i in picks]
        audio_files   = scan_audio()
        any_embedded  = False

        for img in selected_imgs:
            print(f"\n{_ts()} {LV}[i]{R} Processing: {img.name}")
            best_afp, score = _match_img_to_audio(img, audio_files)

            if best_afp and score >= 0.4:
                t, _ = _get_tags(best_afp)
                print(f"  {LV}[i]{R} Match: {t or best_afp.stem}  (score: {score:.0%})")
                konfirm = input(f"  Embed to [{best_afp.name}]? (Y/n): ").strip().lower()
                if konfirm != "n":
                    _crop_square(img)
                    _embed_cover(best_afp, img)
                    any_embedded = True
                    print(f"  {G}[+]{R} Done!")
                    try:
                        img.unlink()
                        print(f"  {DIM}[—] {img.name} deleted.{R}")
                    except Exception as e:
                        print(f"  {Y}[!]{R} Failed to delete {img.name}: {e}")
                    continue

            print(f"  {Y}[!]{R} No automatic match for {img.name}.")
            mau = input(f"  Match manually? (Y/n): ").strip().lower()
            if mau == "n":
                print(f"  {DIM}[~]{R} Skipped.{R}")
                continue
            print(f"\n{V}  ╭═ Pick Song for {img.name} ══════════╮{R}")
            for j, afp in enumerate(audio_files, 1):
                t, a = _get_tags(afp)
                print(f"{V}  │{R}  {P}{j:>2}{R}. {LV}{t or afp.stem}{R}  {DIM}[{a or '—'}]{R}")
            print(f"{V}  ╰{'─' * 42}╯{R}")
            try:
                jpick = int(_ask(f"\n{V}  ❯{R} Number: "))
            except ValueError:
                jpick = 0
            if 1 <= jpick <= len(audio_files):
                target = audio_files[jpick - 1]
                _crop_square(img)
                _embed_cover(target, img)
                any_embedded = True
                print(f"  {G}[+]{R} Done → {target.name}!")
                try:
                    img.unlink()
                    print(f"  {DIM}[—] {img.name} deleted.{R}")
                except Exception as e:
                    print(f"  {Y}[!]{R} Failed to delete {img.name}: {e}")
            else:
                print(f"  {DIM}[~]{R} Skipped.{R}")

        # Android/media players often cache old cover art per file — force a
        # media rescan so the new artwork actually shows up, same as after downloads.
        if any_embedded:
            _refresh_media_scan()
    else:
        # batch match by title tag
        audio_files = scan_audio()
        if not audio_files:
            print(f"\n{Y}[!]{R} {_scan_empty_message()}")
            return
        img_files = []
        for iext in ("*.jpg", "*.jpeg", "*.png", "*.JPG", "*.JPEG", "*.PNG"):
            img_files.extend(Path(".").glob(iext))
        if not img_files:
            print(f"\n{Y}[!]{R} No image files (.jpg/.png) in this folder.")
            return

        print(f"\n{_ts()} {LV}[i]{R} {len(audio_files)} song(s), {len(img_files)} image(s) found.")
        print(f"{LV}[i]{R} Matching based on title tag vs image filename...\n")

        matched, unmatched = [], []
        for afp in audio_files:
            title, _ = _get_tags(afp)
            source_name = title if title else afp.stem
            audio_tokens = _tokenize(source_name)
            best_match, best_score = None, 0.0
            for img in img_files:
                img_tokens = _tokenize(img.stem)
                if not img_tokens:
                    continue
                common = len(audio_tokens & img_tokens)
                union  = len(audio_tokens | img_tokens)
                score  = common / union if union > 0 else 0
                if score > best_score:
                    best_score, best_match = score, img
            if best_match and best_score >= 0.4:
                matched.append((afp, best_match, best_score, source_name))
            else:
                unmatched.append((afp, source_name))

        if matched:
            print("\u2500\u2500 Match results \u2500\u2500")
            for afp, img, score, src in matched:
                print(f"  \u2713 {afp.name}")
                print(f"      Title : {src}")
                print(f"      Image: {img.name}  ({score:.0%} match)")
        if unmatched:
            print("\n\u2500\u2500 No match \u2500\u2500")
            for afp, src in unmatched:
                print(f"  \u2717 {afp.name}  (title: {src})")

        if not matched:
            print(f"\n{Y}[!]{R} No matching pairs found.")
            return

        print()
        skipped, failed = [], []
        for afp, img, score, src in matched:
            konfirm = input(f"Embed [{afp.name}] \u2190 [{img.name}]? (Y/n): ").strip().lower()
            if konfirm == "n":
                skipped.append(afp.name)
                continue
            try:
                _crop_square(img)
                _embed_cover(afp, img)
                print(f"  {G}[+]{R} Done!")
                try:
                    img.unlink()
                    print(f"  {DIM}[—] {img.name} deleted.{R}\n")
                except Exception as de:
                    print(f"  {Y}[!]{R} Failed to delete {img.name}: {de}\n")
            except Exception as e:
                print(f"  {Y}[!]{R} Failed: {e}\n")
                failed.append(afp.name)

        done = len(matched) - len(skipped) - len(failed)
        print(f"\n{_ts()} {G}[+]{R} Finished! {done} succeeded, {len(skipped)} skipped, {len(failed)} error.", end="")
        if failed:
            print()
            for fn in failed:
                print(f"    \u2717 {fn}")
        else:
            print()

        # Force a media rescan so players don't keep showing cached old artwork.
        if done > 0:
            _refresh_media_scan()


M4A_DESC = {
    "\xa9nam": "Title", "\xa9ART": "Artist", "\xa9alb": "Album",
    "\xa9day": "Year",  "\xa9gen": "Genre",  "\xa9lyr": "Lyrics",
    "\xa9cmt": "Comment", "trkn": "Track Number", "disk": "Disc Number",
    "covr": "Cover Art", "aART": "Album Artist", "tmpo": "BPM",
    "cprt": "Copyright",
}

ID3_DESC = {
    "TIT2": "Title",       "TPE1": "Artist",      "TALB": "Album",
    "TDRC": "Year",        "TCON": "Genre",        "TRCK": "Track Number",
    "TPOS": "Disc Number", "TPE2": "Album Artist", "TCOM": "Composer",
    "USLT": "Lyrics",      "SYLT": "Synced Lyrics","COMM": "Comment",
    "APIC": "Cover Art",   "TSSE": "Encoder",      "TLEN": "Length (ms)",
    "WXXX": "Custom URL",  "TXXX": "Custom Tag",   "TBPM": "BPM",
    "TCOP": "Copyright",
}

def check_tags(fp):
    ext = Path(fp).suffix.lower()
    print(f"\n─── Tags: {fp.name} ───")
    if ext == ".m4a":
        from mutagen.mp4 import MP4
        audio = MP4(str(fp))
        if not audio.tags:
            print(f"{Y}[!]{R} No tags.")
            return
        for key, val in audio.tags.items():
            desc = M4A_DESC.get(key, "?")
            if key == "covr":
                print(f"  covr  [{desc}]: <{len(val[0])} bytes>")
            else:
                v = val[0] if len(val) == 1 else val
                print(f"  {key}  [{desc}] = {v}")
    elif ext == ".flac":
        from mutagen.flac import FLAC
        audio = FLAC(str(fp))
        if not audio.tags and not audio.pictures:
            print(f"{Y}[!]{R} No tags.")
            return
        for key, val in (audio.tags or {}).items():
            v = val[0] if len(val) == 1 else val
            print(f"  {key}  = {v}")
        for pic in audio.pictures:
            print(f"  PICTURE  [Cover Art]: <{len(pic.data)} bytes>")
    elif ext == ".opus":
        from mutagen.oggopus import OggOpus
        audio = OggOpus(str(fp))
        pics = _opus_pictures(audio)
        if not audio.tags and not pics:
            print(f"{Y}[!]{R} No tags.")
            return
        for key, val in (audio.tags or {}).items():
            v = val[0] if len(val) == 1 else val
            print(f"  {key}  = {v}")
        for pic in pics:
            print(f"  PICTURE  [Cover Art]: <{len(pic.data)} bytes>")
    else:
        from mutagen.id3 import ID3, APIC
        try:
            tags = ID3(str(fp))
        except Exception:
            print(f"{Y}[!]{R} No ID3 tags.")
            return
        for key, val in tags.items():
            base_key = key.split(":")[0]
            desc = ID3_DESC.get(base_key, "?")
            if isinstance(val, APIC):
                print(f"  {key}  [{desc}]: <{len(val.data)} bytes>")
            else:
                print(f"  {key}  [{desc}] = {val}")


def _parse_multi_input(s, max_n):
    import re
    nums = re.findall(r"\d+", s)
    result = []
    for n in nums:
        i = int(n)
        if 1 <= i <= max_n and i not in result:
            result.append(i)
    return result

def _set_tag_single(fp, tag_type, value):
    ext = Path(fp).suffix.lower()
    if ext == ".m4a":
        from mutagen.mp4 import MP4
        audio = MP4(str(fp))
        if tag_type in ("track", "disc"):
            try:
                parts = str(value).split("/")
                num   = int(parts[0]) if parts[0].strip() else 0
                total = int(parts[1]) if len(parts) > 1 and parts[1].strip() else 0
            except ValueError:
                num, total = 0, 0
            audio["trkn" if tag_type == "track" else "disk"] = [(num, total)]
        else:
            m4a_keys = {
                "title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb",
                "composer": "\xa9wrt", "genre": "\xa9gen", "year": "\xa9day",
                "copyright": "cprt",
            }
            audio[m4a_keys[tag_type]] = [value]
            if tag_type == "artist":
                audio["aART"] = [value]
        audio.save()
    elif ext in (".flac", ".opus"):
        audio = _open_vorbis(fp, ext)
        if tag_type in ("track", "disc"):
            key = "tracknumber" if tag_type == "track" else "discnumber"
            audio[key] = [str(value)]
        else:
            vorbis_keys = {
                "title": "title", "artist": "artist", "album": "album",
                "composer": "composer", "genre": "genre", "year": "date",
                "copyright": "copyright",
            }
            audio[vorbis_keys[tag_type]] = [value]
            if tag_type == "artist":
                audio["albumartist"] = [value]
        audio.save()
    else:
        from mutagen.id3 import ID3, TIT2, TPE1, TALB, TPE2, TCOM, TCON, TDRC, TRCK, TPOS, TCOP
        audio = ID3(str(fp))
        if tag_type in ("track", "disc"):
            key, cls = ("TRCK", TRCK) if tag_type == "track" else ("TPOS", TPOS)
            audio[key] = cls(encoding=3, text=str(value))
        else:
            cls_map = {
                "title": ("TIT2", TIT2), "artist": ("TPE1", TPE1), "album": ("TALB", TALB),
                "composer": ("TCOM", TCOM), "genre": ("TCON", TCON), "year": ("TDRC", TDRC),
                "copyright": ("TCOP", TCOP),
            }
            key, cls = cls_map[tag_type]
            audio[key] = cls(encoding=3, text=value)
            if tag_type == "artist":
                audio["TPE2"] = TPE2(encoding=3, text=value)
        audio.save()
    _history_log("edit", f"{tag_type.capitalize()} set to \"{value}\"", file=Path(fp).name)

def _batch_set_simple(tag_type, label):
    """Simple batch editor for the newer fields (Composer/Genre/Year/Track/Disc/
    Copyright): pick multiple files, enter ONE value, apply to all. No
    auto-detect-group magic here -- that stays specific to Artist/Album in
    batch_set_tag(), since grouping by existing Composer/Genre/Track value
    isn't a meaningfully common use case."""
    files = scan_audio()
    if not files:
        print(f"\n{Y}[!]{R} {_scan_empty_message('No audio files in this folder.')}")
        return
    print(f"\n{V}  ╭═ Pick File(s) (multi) ══════════════════════════{R}")
    for i, afp in enumerate(files, 1):
        t, a = _get_tags(afp)
        print(f"{V}  │{R}  {P}{i:>2}{R}. {LV}{t or afp.stem}{R}  {DIM}[{a or '—'}]{R}")
    print(f"{V}  ╰{'=' * 42}{R}")
    raw = _ask(f"\n{V}  ❯{R} Number(s) (e.g., 1,4,5): ").strip()
    picks = _parse_multi_input(raw, len(files))
    if not picks:
        print(f"  {Y}[!]{R} No valid numbers.")
        return
    selected = [files[i - 1] for i in picks]
    value = input(f"\n  New {label}: ").strip()
    if not value:
        print(f"  {DIM}Cancelled.{R}")
        return
    ok, fail = 0, []
    for afp in selected:
        try:
            _set_tag_single(afp, tag_type, value)
            _stamp_comment(afp, "Edited & Embedded by TagMan")
            ok += 1
        except Exception as e:
            fail.append((afp.name, str(e)))
    print(f"\n  {G}[+]{R} {ok} file(s) updated → {label}: {value}")
    for name, err in fail:
        print(f"  {Y}[!]{R} Failed {name}: {err}")

def batch_set_tag(tag_type):
    label = "Artist" if tag_type == "artist" else "Album"

    artist_mode = "both"
    if tag_type == "artist":
        _sub_box("Batch Change Artist", [
            (1, "Artist + Album Artist", "both"),
            (2, "Album Artist only",     None),
            (3, "Artist only",           None),
            (0, "Back",                  None),
        ], width=48, icon="✎")
        try:
            msub = int(_ask(f"\n{V}  ❯{R} Pick: "))
        except ValueError:
            return
        if msub == 0:
            return
        if msub not in (1, 2, 3):
            print(f"  {Y}[!]{R} Invalid choice.")
            return
        artist_mode = {1: "both", 2: "aa_only", 3: "artist_only"}[msub]
        label = {"both": "Artist + Album Artist", "aa_only": "Album Artist",
                  "artist_only": "Artist"}[artist_mode]

    files = scan_audio()
    if not files:
        print(f"\n{Y}[!]{R} {_scan_empty_message('No audio files in this folder.')}")
        return

    from collections import defaultdict

    def _primary(key):
        """Extract main artist name (before comma/slash/feat/&) for grouping."""
        if tag_type != "artist":
            return key
        import re
        parts = re.split(r"[,/&]|\bfeat\.?\b|\bft\.?\b|\bx\b", key, flags=re.IGNORECASE)
        return parts[0].strip() if parts else key

    groups = defaultdict(list)
    for afp in files:
        title, artist = _get_tags(afp)
        if tag_type == "artist":
            key = _primary(artist.strip()) if artist.strip() else ""
        else:
            ext = afp.suffix.lower()
            try:
                if ext == ".m4a":
                    from mutagen.mp4 import MP4
                    audio = MP4(str(afp))
                    key = str(audio.tags.get("\xa9alb", [""])[0]).strip() if audio.tags else ""
                elif ext in (".flac", ".opus"):
                    audio = _open_vorbis(afp, ext)
                    key = str(audio.get("album", [""])[0]).strip() if audio.tags else ""
                else:
                    from mutagen.id3 import ID3
                    tags = ID3(str(afp))
                    key = str(tags.get("TALB", "")).strip()
            except Exception:
                key = ""
        groups[key or afp.stem].append(afp)

    valid_groups = {k: v for k, v in groups.items() if len(v) >= 2}
    selected_files = None
    new_value      = None

    if valid_groups:
        print(f"\n{V}  ╭═ Auto-detect {label} Groups ═══════════════════{R}")
        group_list = sorted(valid_groups.items(), key=lambda x: -len(x[1]))
        for i, (gname, gfiles) in enumerate(group_list, 1):
            print(f"{V}  │{R}  {P}{i}{R}. {LV}{gname}{R}  {DIM}({len(gfiles)} song(s)){R}")
            for afp in gfiles[:4]:
                t, _ = _get_tags(afp)
                print(f"{V}  │{R}     {DIM}• {t or afp.stem}{R}")
            if len(gfiles) > 4:
                print(f"{V}  │{R}     {DIM}... +{len(gfiles)-4} more{R}")
        print(f"{V}  │{R}  {P}0{R}. {DIM}Skip / manual input{R}")
        print(f"{V}  ╰{'=' * 42}{R}")

        try:
            pick = int(_ask(f"\n{V}  ❯{R} Pick group: "))
        except ValueError:
            pick = 0

        if 1 <= pick <= len(group_list):
            gname, gfiles = group_list[pick - 1]
            print(f"\n{DIM}  Songs to update:{R}")
            for afp in gfiles:
                t, _ = _get_tags(afp)
                print(f"  {DIM}• {t or afp.stem}{R}")
            konfirm = input(f"\n  Update {len(gfiles)} song(s)? (Y/n): ").strip().lower()
            if konfirm != "n":
                new_value = input(f"  New {label}: ").strip()
                if new_value:
                    selected_files = gfiles

    if selected_files is None:
        if valid_groups:
            mau = input(f"\n  Use manual number input? (Y/n): ").strip().lower()
            if mau == "n":
                return

        print(f"\n{V}  ╭═ Pick File(s) (multi) ══════════════════════════{R}")
        for i, afp in enumerate(files, 1):
            t, a = _get_tags(afp)
            print(f"{V}  │{R}  {P}{i:>2}{R}. {LV}{t or afp.stem}{R}  {DIM}[{a or '—'}]{R}")
        print(f"{V}  ╰{'=' * 42}{R}")
        raw = _ask(f"\n{V}  ❯{R} Number(s) (e.g., 1,4,5): ").strip()
        picks = _parse_multi_input(raw, len(files))
        if not picks:
            print(f"  {Y}[!]{R} No valid numbers.")
            return
        selected_files = [files[i-1] for i in picks]
        print(f"\n  {DIM}Selected files:{R}")
        for afp in selected_files:
            t, _ = _get_tags(afp)
            print(f"  {DIM}• {t or afp.stem}{R}")
        new_value = input(f"  New {label}: ").strip()

    if not new_value or not selected_files:
        print(f"  {DIM}Cancelled.{R}")
        return

    ok, fail = 0, []
    for afp in selected_files:
        try:
            if tag_type == "artist":
                _set_artist_field(afp, new_value, artist_mode)
            else:
                _set_tag_single(afp, tag_type, new_value)
            _stamp_comment(afp, "Edited & Embedded by TagMan")
            ok += 1
        except Exception as e:
            fail.append((afp.name, str(e)))

    mode_label = {"both": "Artist + Album Artist", "aa_only": "Album Artist",
                  "artist_only": "Artist"}.get(artist_mode, "Artist")
    out_label  = mode_label if tag_type == "artist" else label
    print(f"\n  {G}[+]{R} {ok} file(s) updated → {out_label}: {new_value}")
    for name, err in fail:
        print(f"  {Y}[!]{R} Failed {name}: {err}")


def _set_artist_field(fp, value, mode):
    """mode='both' (artist+album artist), 'aa_only' (album artist only), or
    'artist_only' (artist only, leaves Album Artist untouched)."""
    ext = Path(fp).suffix.lower()
    if ext == ".m4a":
        from mutagen.mp4 import MP4
        audio = MP4(str(fp))
        if mode in ("both", "artist_only"):
            audio["\xa9ART"] = [value]
        if mode in ("both", "aa_only"):
            audio["aART"] = [value]
        audio.save()
    elif ext in (".flac", ".opus"):
        audio = _open_vorbis(fp, ext)
        if mode in ("both", "artist_only"):
            audio["artist"] = [value]
        if mode in ("both", "aa_only"):
            audio["albumartist"] = [value]
        audio.save()
    else:
        from mutagen.id3 import ID3, TPE1, TPE2
        audio = ID3(str(fp))
        if mode in ("both", "artist_only"):
            audio["TPE1"] = TPE1(encoding=3, text=value)
        if mode in ("both", "aa_only"):
            audio["TPE2"] = TPE2(encoding=3, text=value)
        audio.save()
    if mode == "both":
        print(f"  {G}[+]{R} Artist set to: {value}")
        print(f"  {DIM}[~] Album Artist also updated → {value}{R}")
    elif mode == "artist_only":
        print(f"  {G}[+]{R} Artist set to: {value}")
    else:
        print(f"  {G}[+]{R} Album Artist set to: {value}")


def set_tag(fp, tag_type):
    while True:
        if tag_type == "artist":
            print(f"\n{V}  ╔═ Change Artist ════════════════════════════════╗{R}")
            print(f"{V}  ║{R}  {P}1{R}. {LV}Artist + Album Artist{R}   {DIM}(both){R}")
            print(f"{V}  ║{R}  {P}2{R}. {LV}Album Artist only{R}")
            print(f"{V}  ║{R}  {P}3{R}. {LV}Artist only{R}")
            print(f"{V}  ║{R}  {P}0{R}. {DIM}Back{R}")
            print(f"{V}  ╚{'=' * 48}╝{R}")
            try:
                sub = int(_ask(f"\n{V}  ❯{R} Pick: "))
            except ValueError:
                return
            if sub == 0:
                return
            if sub not in (1, 2, 3):
                print(f"  {Y}[!]{R} Invalid choice.")
                return
            mode  = {1: "both", 2: "aa_only", 3: "artist_only"}[sub]
            label = {"both": "Artist + Album Artist", "aa_only": "Album Artist",
                      "artist_only": "Artist"}[mode]
            value = input(f"\n  New {label}: ").strip()
            if not value:
                print("Cancelled.")
                return
            _, current_artist = _get_tags(fp)
            print(f"\n  {DIM}Now:{R} {current_artist or '—'}")
            print(f"  {DIM}New: {R}{G}{value}{R}")
            konfirm = input(f"\n  Apply this change? (Y/n): ").strip().lower()
            if konfirm == "n":
                print(f"  {DIM}Cancelled — nothing written.{R}")
                return
            _set_artist_field(fp, value, mode)
            _stamp_comment(fp, "Edited & Embedded by TagMan")
            lanjut = input(f"\n  Edit another file? (Y/n): ").strip().lower()
            if lanjut == "n":
                return
            fp2 = pick_file()
            if fp2 is None:
                return
            fp = fp2
            continue

        label = {
            "title": "Title", "album": "Album", "composer": "Composer",
            "genre": "Genre", "year": "Year", "track": "Track Number",
            "disc": "Disc Number", "copyright": "Copyright",
        }[tag_type]
        value = input(f"New {label}: ").strip()
        if not value:
            print("Cancelled.")
            return
        konfirm = input(f"\n  Set {label} to '{value}'? (Y/n): ").strip().lower()
        if konfirm == "n":
            print(f"  {DIM}Cancelled — nothing written.{R}")
            return

        _set_tag_single(fp, tag_type, value)

        _stamp_comment(fp, "Edited & Embedded by TagMan")
        print(f"{G}[+]{R} {label} set to: {value}")
        lanjut = input(f"\n  Edit another file? (Y/n): ").strip().lower()
        if lanjut == "n":
            return
        fp2 = pick_file(show_album=(tag_type == "album"))
        if fp2 is None:
            return
        fp = fp2




# ─── lyrics helpers ───────────────────────────────────────────────────────────

def _read_common_tags(fp):
    """Read title, artist, album_artist, album from .mp3/.m4a/.flac/.opus. Returns dict with empty strings on failure."""
    result = {"title": "", "artist": "", "album_artist": "", "album": ""}
    ext = Path(fp).suffix.lower()
    try:
        if ext == ".m4a":
            from mutagen.mp4 import MP4
            audio = MP4(str(fp))
            tags = audio.tags
            result["title"]        = str(tags.get("\xa9nam", [""])[0]).strip() if tags else ""
            result["artist"]       = str(tags.get("\xa9ART", [""])[0]).strip() if tags else ""
            result["album_artist"] = str(tags.get("aART",    [""])[0]).strip() if tags else ""
            result["album"]        = str(tags.get("\xa9alb", [""])[0]).strip() if tags else ""
        elif ext in (".flac", ".opus"):
            audio = _open_vorbis(fp, ext)
            tags = audio.tags
            result["title"]        = str(tags.get("title",       [""])[0]).strip() if tags else ""
            result["artist"]       = str(tags.get("artist",      [""])[0]).strip() if tags else ""
            result["album_artist"] = str(tags.get("albumartist", [""])[0]).strip() if tags else ""
            result["album"]        = str(tags.get("album",       [""])[0]).strip() if tags else ""
        else:
            from mutagen.id3 import ID3
            tags = ID3(str(fp))
            result["title"]        = str(tags.get("TIT2", "")).strip()
            result["artist"]       = str(tags.get("TPE1", "")).strip()
            result["album_artist"] = str(tags.get("TPE2", "")).strip()
            result["album"]        = str(tags.get("TALB", "")).strip()
    except Exception:
        pass
    return result

def _get_artist_album(fp):
    """Return (artist, album_artist, album) for preview."""
    t = _read_common_tags(fp)
    return t["artist"] or "—", t["album_artist"] or "—", t["album"]

def _get_tags(fp):
    """Return (title, artist) from tags."""
    t = _read_common_tags(fp)
    return t["title"], t["artist"]

_FEAT_RE = re.compile(
    r"[\(\[]?\s*(?:feat\.?|ft\.?|featuring)\s+([^\)\]]+)[\)\]]?",
    re.IGNORECASE,
)

_TITLE_PREFIX_RE = re.compile(r"^\s*([^-–—]+?)\s[-–—]\s+(.+)$")

# Words/phrases that show up after a dash as a *version/edit descriptor*,
# not a song title -- e.g. "Bad Liar - Stripped" or "Halo - Live Acoustic".
# When the part after the dash matches one of these, the text is really
# "Song - Version" and NOT the "Artist - Song" shape the prefix method
# looks for, so we must not mistake the song name for an artist name.
_VERSION_WORDS = {
    "stripped", "acoustic", "live", "remix", "remixed", "extended", "remaster",
    "remastered", "demo", "cover", "instrumental", "unplugged", "sped", "up",
    "slowed", "reverb", "nightcore", "karaoke", "clean", "explicit", "mono",
    "stereo", "bonus", "track", "edit", "mix", "session", "alt", "alternate",
    "bootleg", "mashup", "freestyle", "dub", "vip", "8d", "audio", "lofi",
    "lo-fi", "chopped", "screwed", "radio", "original", "piano", "studio",
    "album", "single", "deluxe", "reprise", "outro", "intro", "tv", "main",
    "version", "cut", "take", "+", "&", "and", "with",
}

def _looks_like_version_tag(song):
    """True if `song` reads as a version/edit descriptor ('Stripped',
    'Live Acoustic', 'Sped Up + Reverb') rather than an actual song title --
    i.e. every word in it is a known version-tag word, or it's a bare
    4-digit remaster year like '2021 Remaster'."""
    song = song.strip()
    if not song:
        return False
    if re.match(r"^\d{4}\s+(remaster(?:ed)?|version|mix|edit)$", song, re.IGNORECASE):
        return True
    words = re.findall(r"[A-Za-z+&]+", song.lower())
    if not words:
        return False
    return all(w in _VERSION_WORDS for w in words)

_NAME_LIST_SPLIT_RE = re.compile(r",|&|\s/\s|\bx\b|\band\b", re.IGNORECASE)

def _split_name_list(raw):
    """Split a comma/&/x/and-separated artist list into names. A bare
    slash with no surrounding spaces (e.g. 'Au/Ra', 'AC/DC') is treated as
    part of the name rather than a separator -- only a *spaced* slash
    ('Alan Walker / Au Ra') is treated as one."""
    parts = _NAME_LIST_SPLIT_RE.split(raw)
    return [p.strip(" .") for p in parts if p.strip(" .")]

def _extract_featured_artists(text):
    """Look for a 'feat./ft./featuring' clause inside a title (or filename
    fallback) string and return the list of names it lists, e.g.
    'Baby (feat. Marina, Luis Fonsi)' -> ['Marina', 'Luis Fonsi']. Returns
    [] if no such clause is present. Lower confidence than the title-prefix
    method below -- it's inferring extra artists from a parenthetical,
    not an explicit list -- so validation only falls back to this when
    there's no prefix to go on."""
    if not text:
        return []
    m = _FEAT_RE.search(text)
    if not m:
        return []
    raw = m.group(1).strip()
    return _split_name_list(raw)

def _extract_title_prefix_artists(text):
    """Look for a 'Artist1, Artist2 - Song Name' shape -- the format this
    app's own downloader/query builder uses (see `query = f"{artist} -
    {title}"` in the lyrics fetch flow), and commonly how YouTube-sourced
    titles/filenames come in. Returns the list of names listed before the
    dash, e.g. 'Clean Bandit, MARINA, Luis Fonsi - Baby' -> ['Clean
    Bandit', 'MARINA', 'Luis Fonsi']. Returns [] if the text doesn't look
    like that, or if the part before the dash still contains a 'feat./ft.'
    clause (then it isn't a clean artist list)."""
    if not text:
        return []
    m = _TITLE_PREFIX_RE.match(text)
    if not m:
        return []
    prefix = m.group(1).strip()
    song = m.group(2).strip()
    if not prefix or _FEAT_RE.search(prefix):
        return []
    if _looks_like_version_tag(song):
        # e.g. "Bad Liar - Stripped": the part after the dash is a version/
        # edit descriptor, not a song title, so this is "Song - Version",
        # not "Artist - Song". Don't mistake the song name for an artist.
        return []
    return _split_name_list(prefix)

def _validation_scan(files):
    """Compare each file's Artist tag against artist names implied by its
    Title (falling back to the filename if Title is blank), using two
    detection methods tried in priority order:
      1. Title prefix -- 'Artist1, Artist2 - Song' (near-certain signal).
      2. '(feat. ...)' clause inside the title -- used only when there's
         no prefix to go on; a real signal, but less certain than an
         explicit artist list.
    Returns a list of (fp, current_artist, missing_names, proposed_artist,
    source_text, method, action) for every file where the Artist tag looks
    incomplete or wrong. `method` is 'prefix', 'feat', or 'both', for
    display purposes. `action` is 'add' when the current artist overlaps
    with what was detected (just missing a collaborator) or 'replace' when
    none of it overlaps (the current tag looks unrelated/wrong outright)."""
    issues = []
    for afp in files:
        tags   = _read_common_tags(afp)
        artist = tags["artist"].strip()
        if not artist:
            continue  # nothing to anchor a fix to -- skip rather than guess

        # Check the Title tag *and* the filename -- not just the filename
        # as a fallback when Title is blank. A file can have a clean Title
        # ("Bye Bye Bye") that hides an artist mismatch that the filename
        # ("*NSYNC - Bye Bye Bye.m4a") still reveals, so both are worth a
        # look. Title is checked first since it's the more curated field.
        title_text = tags["title"].strip()
        name_text  = afp.stem
        sources = [s for s in (title_text, name_text) if s]
        # de-dupe while preserving order (title == filename stem is common)
        seen = set()
        sources = [s for s in sources if not (s in seen or seen.add(s))]

        candidates, method, source = [], None, None
        for src in sources:
            prefix_names = _extract_title_prefix_artists(src)
            feat_names   = _extract_featured_artists(src)
            # Combine both signals from the same source text -- a title can
            # legitimately have a prefix list *and* a separate feat. clause
            # ("Katy Perry - California Gurls (feat. Snoop Dogg)"), and only
            # checking prefix would silently drop Snoop Dogg.
            combined = list(dict.fromkeys(prefix_names + feat_names))
            if combined:
                if prefix_names and feat_names:
                    method = "both"
                elif prefix_names:
                    method = "prefix"
                else:
                    method = "feat"
                candidates, source = combined, src
                break
        if not candidates:
            continue

        artist_lower = artist.lower()
        missing = [n for n in candidates if n.lower() not in artist_lower]
        if not missing:
            continue

        if len(missing) == len(candidates):
            # None of the detected names overlap with the current Artist
            # tag at all -- e.g. Artist="Jerusalem" but the title/filename
            # says "*NSYNC". That's not a missing collaborator, it's an
            # unrelated/wrong tag, so proposing "Jerusalem, *NSYNC" would
            # just bolt the detected name onto garbage. Propose replacing
            # the tag outright instead.
            action = "replace"
            proposed = ", ".join(candidates)
        else:
            action = "add"
            proposed = artist + ", " + ", ".join(missing)
        issues.append((afp, artist, missing, proposed, source, method, action))
    return issues


def _validation_fix_artist_issues(issues):
    """Report + offer-to-fix flow for the Artist-tag-vs-title/filename part
    of Validation. `issues` is the list _validation_scan() returns. Never
    touches Album Artist -- only ever writes the Artist field -- and always
    confirms before writing.

    Two fix methods, picked once for the whole selected batch:
      1. Automatic -- use the name(s) validation already detected (from the
         title prefix, or the feat. clause as fallback).
      2. Manual -- type the Artist value yourself, per file, with the
         Title/filename shown alongside as a reference."""
    method_label = {"prefix": "title prefix", "feat": "feat. clause", "both": "title prefix + feat."}
    rows = []
    for i, (afp, artist, missing, proposed, source, method, action) in enumerate(issues, 1):
        name = _fit_name(afp.name)
        tag  = f"{method_label[method]}, replace" if action == "replace" else method_label[method]
        rows.append((f"  {i:>2}. {name}", f"  {P}{i:>2}{R}. {LV}{name}{R}"))
        rows.append((f"      Now:  {artist}", f"      {DIM}Now:  {R}{artist}"))
        rows.append((f"      New:  {proposed}  ({tag})",
                      f"      {DIM}New:  {R}{G}{proposed}{R}  {DIM}({tag}){R}"))
    _box("Validation — Artist Tag Issues", rows, min_width=50, icon="✓")

    raw = _ask_optional(f"\n{V}  ❯{R} Fix which? Number(s) (e.g., 1,3,5), 'a' for all, or Enter to skip: ").strip()
    if not raw or raw == "0":
        print(f"  {DIM}Skipped.{R}")
        return
    if raw.lower() == "a":
        picks = list(range(1, len(issues) + 1))
    else:
        picks = _parse_multi_input(raw, len(issues))
    if not picks:
        print(f"  {Y}[!]{R} No valid numbers.")
        return
    selected = [issues[i - 1] for i in picks]

    _sub_box("Fix Method", [
        (1, "Automatic", "use the name(s) detected from Title/filename"),
        (2, "Manual",    "type the Artist yourself, per file"),
        (0, "Back",      None),
    ], width=56, icon="✓")
    try:
        method_choice = int(_ask(f"\n{V}  ❯{R} Pick: "))
    except (ValueError, EOFError):
        method_choice = 0
    if method_choice == 0:
        print(f"  {DIM}Cancelled.{R}")
        return
    if method_choice not in (1, 2):
        print(f"  {Y}[!]{R} Invalid choice.")
        return

    if method_choice == 1:
        print(f"\n{DIM}  About to update:{R}")
        for afp, artist, missing, proposed, source, method, action in selected:
            print(f"  {DIM}• {afp.name}{R}")
            tag = f"  {Y}(replace){R}" if action == "replace" else ""
            print(f"    {artist}  {DIM}->{R}  {G}{proposed}{R}{tag}")
        konfirm = input(f"\n  Apply {len(selected)} change(s)? (Y/n): ").strip().lower()
        if konfirm == "n":
            print(f"  {DIM}Cancelled.{R}")
            return

        ok, fail = 0, []
        for afp, artist, missing, proposed, source, method, action in selected:
            try:
                _set_artist_field(afp, proposed, "artist_only")
                _stamp_comment(afp, "Edited & Embedded by TagMan")
                ok += 1
            except Exception as e:
                fail.append((afp.name, str(e)))
        print(f"\n  {G}[+]{R} {ok} file(s) updated.")
        for name, err in fail:
            print(f"  {Y}[!]{R} Failed {name}: {err}")
        return

    # method_choice == 2: manual, per file
    ok, skipped = 0, 0
    for afp, artist, missing, proposed, source, method, action in selected:
        print(f"\n{DIM}  ── {afp.name} ──{R}")
        print(f"  {DIM}Title/filename ref: {R}{source}")
        print(f"  {DIM}Current Artist:     {R}{artist}")
        value = input("  New Artist (Enter to skip this file): ").strip()
        if not value:
            print(f"  {DIM}Skipped.{R}")
            skipped += 1
            continue
        print(f"  {DIM}New: {R}{G}{value}{R}")
        konfirm = input("  Apply? (Y/n): ").strip().lower()
        if konfirm == "n":
            print(f"  {DIM}Skipped.{R}")
            skipped += 1
            continue
        try:
            _set_artist_field(afp, value, "artist_only")
            _stamp_comment(afp, "Edited & Embedded by TagMan")
            print(f"  {G}[+]{R} Artist set to: {value}")
            ok += 1
        except Exception as e:
            print(f"  {Y}[!]{R} Failed: {e}")
    print(f"\n  {G}[+]{R} {ok} file(s) updated.  {DIM}({skipped} skipped){R}")


def _validation_fix_lyrics_issues(lyrics_issues):
    """Report + offer-to-fetch flow for the lyrics part of Validation.
    `lyrics_issues` is a list of (fp, status) where status is 'missing' or
    'placeholder'. Reuses the exact same interactive fetch as the Lyrics
    menu's auto-fetch-single mode."""
    rows = []
    for i, (afp, status) in enumerate(lyrics_issues, 1):
        name  = _fit_name(afp.name)
        label = "No lyrics found" if status == "missing" else "Placeholder only (TN) — no real lyrics"
        rows.append((f"  {i:>2}. {name}", f"  {P}{i:>2}{R}. {LV}{name}{R}"))
        rows.append((f"      {label}", f"      {DIM}{label}{R}"))
    _box("Validation — Lyrics Issues", rows, min_width=50, icon="♪")

    raw = _ask_optional(f"\n{V}  ❯{R} Fetch lyrics for which? Number(s), 'a' for all, or Enter to skip: ").strip()
    if not raw or raw == "0":
        print(f"  {DIM}Skipped.{R}")
        return
    if raw.lower() == "a":
        picks = list(range(1, len(lyrics_issues) + 1))
    else:
        picks = _parse_multi_input(raw, len(lyrics_issues))
    if not picks:
        print(f"  {Y}[!]{R} No valid numbers.")
        return

    selected = [lyrics_issues[i - 1][0] for i in picks]
    _sub_box("Provider Priority", [
        (1, "Normal",              "syncedlyrics first"),
        (2, "Prioritize LRCLib",   "LRCLib first"),
    ], width=50, icon="♪")
    try:
        provider_choice = int(_ask(f"\n{V}  ❯{R} Pick (1/2): "))
    except ValueError:
        provider_choice = 1
    prefer_lrclib = (provider_choice == 2)

    _print_patience_banner()
    for fp in selected:
        print(f"\n{DIM}── {fp.name} ──{R}")
        _fetch_lyrics_for_file(fp, prefer_lrclib=prefer_lrclib)


def run_validation():
    """Main-menu 'Validation': a read-only scan of the current folder that
    reports two kinds of issues, then optionally offers to fix each --
      1. Artist tag vs Title/filename: flags files whose Artist tag is
         missing a featuring artist that shows up in the Title (falling
         back to the filename if Title is blank), e.g. Artist = 'Clean
         Bandit' but the title reads '... (feat. Sean Paul, Anne-Marie)'.
      2. Lyrics: flags files with no lyrics at all, or with only the 'TN'
         placeholder _embed_lyrics() stamps when nothing was found/entered.
    The scan itself never writes anything -- it only reports -- and each
    category is fixed independently, always with a confirmation step
    before anything is written."""
    files = scan_audio()
    if not files:
        print(f"\n{Y}[!]{R} {_scan_empty_message('No audio files in this folder.')}")
        return

    artist_issues = _validation_scan(files)
    lyrics_issues = [(afp, status) for afp in files
                      if (status := _lyrics_validation_status(afp))]

    if not artist_issues and not lyrics_issues:
        print(f"\n  {G}[+]{R} Validation complete — everything looks good.")
        return

    if artist_issues:
        _validation_fix_artist_issues(artist_issues)
    else:
        print(f"\n  {G}[+]{R} Artist tags all look correct.")

    if lyrics_issues:
        _validation_fix_lyrics_issues(lyrics_issues)
    else:
        print(f"\n  {G}[+]{R} All files have lyrics.")

def _get_duration(fp):
    """Return track duration in whole seconds, or None if it can't be read.
    Used to ask LRCLib for the exact, duration-verified match instead of
    guessing from a loosely-ranked search list."""
    try:
        from mutagen import File as MutagenFile
        audio = MutagenFile(str(fp))
        if audio is not None and audio.info and audio.info.length:
            return int(round(audio.info.length))
    except Exception:
        pass
    return None

def _stamp_comment(fp, message, force=False):
    """Write TagMan comment, unless an existing comment already contains 'tagman'
    (case-insensitive). force=True: always overwrite."""
    ext = Path(fp).suffix.lower()
    try:
        if ext == ".m4a":
            from mutagen.mp4 import MP4
            audio    = MP4(str(fp))
            existing = str(audio.get("\xa9cmt", [""])[0]) if audio.get("\xa9cmt") else ""
            if not force and "tagman" in existing.lower():
                return
            audio["\xa9cmt"] = [message]
            audio.save()
        elif ext in (".flac", ".opus"):
            audio    = _open_vorbis(fp, ext)
            existing = str(audio.get("comment", [""])[0]) if audio.get("comment") else ""
            if not force and "tagman" in existing.lower():
                return
            audio["comment"] = [message]
            audio.save()
        else:
            from mutagen.id3 import ID3, COMM
            audio    = ID3(str(fp))
            comm     = audio.get("COMM::eng") or audio.get("COMM::")
            existing = str(comm.text[0]) if comm and comm.text else ""
            if not force and "tagman" in existing.lower():
                return
            audio["COMM::eng"] = COMM(encoding=3, lang="eng", desc="", text=message)
            audio.save()
    except Exception:
        pass

def _ensure_fallback_fields(fp, fields=("copyright",), fallback="TN"):
    """For each given TEXT field (copyright/composer/genre), fill with `fallback`
    if it's empty or yt-dlp's "NA" missing-field placeholder. Never overwrites a
    genuine value. Used after Sorcerer downloads, since YouTube Music often just
    doesn't have this info at all -- same reasoning as the copyright fallback.
    Track/Disc aren't included: those are numeric-tuple fields in M4A, so "TN"
    literally can't be stored there -- they're just left unset if unavailable,
    same as any other missing numeric tag."""
    def _is_blank(v):
        v = v.strip(" /")
        if not v:
            return True
        return all(tok.upper() == "NA" for tok in v.split())

    ext = Path(fp).suffix.lower()
    m4a_keys = {"copyright": "cprt", "composer": "\xa9wrt", "genre": "\xa9gen"}
    try:
        if ext == ".m4a":
            from mutagen.mp4 import MP4
            audio = MP4(str(fp))
            for f in fields:
                key = m4a_keys.get(f)
                if not key:
                    continue
                val = str(audio.get(key, [""])[0]) if audio.get(key) else ""
                if _is_blank(val):
                    audio[key] = [fallback]
            audio.save()
        elif ext in (".flac", ".opus"):
            flac_keys = {"copyright": "copyright", "composer": "composer", "genre": "genre"}
            audio = _open_vorbis(fp, ext)
            for f in fields:
                key = flac_keys.get(f)
                if not key:
                    continue
                val = str(audio.get(key, [""])[0]) if audio.get(key) else ""
                if _is_blank(val):
                    audio[key] = [fallback]
            audio.save()
        else:
            cls_map = {"copyright": ("TCOP", TCOP), "composer": ("TCOM", TCOM), "genre": ("TCON", TCON)}
            audio = ID3(str(fp))
            for f in fields:
                if f not in cls_map:
                    continue
                key, cls = cls_map[f]
                cur = audio.get(key)
                val = str(cur.text[0]) if cur and cur.text else ""
                if _is_blank(val):
                    audio[key] = cls(encoding=3, text=fallback)
            audio.save()
    except Exception:
        pass

def _ensure_album_fallback(fp):
    """If a downloaded track has no album tag -- common for a plain YouTube
    video that was never released as a 'song'/album on YTMusic -- fall back
    to the track's own title tag, or the filename (minus the leading
    'Uploader - ' bit) if even the title tag is missing. Never overwrites a
    genuine album value. This is a safety net on top of the --parse-metadata
    fallback chain in _YT_META_FLAGS, in case that pass didn't take (e.g.
    some MP3 postprocessing quirks)."""
    def _is_blank(v):
        v = (v or "").strip(" /")
        if not v:
            return True
        return all(tok.upper() == "NA" for tok in v.split())

    tags = _read_common_tags(fp)
    if not _is_blank(tags.get("album", "")):
        return  # already has a real album name, leave it alone

    fallback = (tags.get("title") or "").strip()
    if _is_blank(fallback):
        fallback = Path(fp).stem
        # downloaded files are named "Uploader - Title.ext" -- strip the
        # "Uploader - " prefix so the album name matches the song title,
        # not the whole filename.
        if " - " in fallback:
            fallback = fallback.split(" - ", 1)[1]
    fallback = fallback.strip()
    if not fallback:
        return

    try:
        _set_tag_single(fp, "album", fallback)
    except Exception:
        pass

_LAST_LYRICS_SOURCE = None   # set by _lrclib_search/_fetch_lyrics; read right after fetching
_LAST_LYRICS_IS_PLAIN = False  # True if the lyrics returned have no synced timestamps
_LAST_LYRICS_ERROR = False     # True if the last fetch failed because of a connection
                                # problem (DNS/timeout/refused) rather than a genuine miss

def _is_conn_error(exc):
    """True if exc looks like a network/connection failure (no internet, DNS
    down, timeout, refused, etc.) rather than e.g. a bad-response/parse error."""
    import urllib.error, socket
    return isinstance(exc, (urllib.error.URLError, socket.timeout, ConnectionError, OSError))

def _looks_synced(text):
    """Heuristic: True only if `text` actually contains LRC-style [mm:ss.xx]
    timestamp tags (at least a couple, to avoid false positives from a
    single stray bracket). syncedlyrics.search() can silently return
    plain (non-timed) lyrics from some of its backend providers, so its
    output can never just be assumed synced -- it has to be checked."""
    if not text:
        return False
    import re
    return len(re.findall(r"\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]", text)) >= 2

def _lrclib_search(query, duration=None):
    """Search lyrics on LRCLib. Returns (lyrics, is_plain) or (None, False).

    Root cause of the 'Prioritize LRCLib' mode silently returning
    mismatched/imprecise timings: /api/search returns a loosely-ranked list of
    candidate entries (covers, karaoke, re-uploads with slightly different
    sync, even wrong songs), and grabbing "the first one with syncedLyrics"
    does not mean it's the one LRCLib itself considers correct.

    LRCLib's /api/get endpoint (artist + track + duration, ±2s tolerance) is
    what lrclib.net's own "exact match" logic uses — it returns ONE canonical,
    duration-verified entry. That's tried first whenever we know the file's
    duration. Only if that misses do we fall back to /api/search, and even
    then prefer entries whose duration is close to ours.

    A plain-only exact match is kept as a fallback rather than returned right
    away, so /api/search still gets a chance to turn up a synced version —
    callers only ever get plain lyrics here when nothing synced exists at all.
    """
    global _LAST_LYRICS_SOURCE, _LAST_LYRICS_ERROR
    import urllib.request, urllib.parse, json

    def _try(endpoint, params):
        global _LAST_LYRICS_ERROR
        url = f"https://lrclib.net/api/{endpoint}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "tagman/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            if _is_conn_error(e):
                _LAST_LYRICS_ERROR = True
            raise

    parts = query.split(" - ", 1)
    artist_q = title_q = None
    if len(parts) == 2:
        artist_q, title_q = parts[0].strip(), parts[1].strip()

    plain_fallback = None  # (lyrics, source) — verified exact match, but plain only

    # ── Exact match first: this is the entry LRCLib itself treats as "the" match ──
    if duration and artist_q and title_q:
        try:
            entry = _try("get", {
                "artist_name": artist_q,
                "track_name":  title_q,
                "duration":    duration,
            })
            if isinstance(entry, dict):
                sl = entry.get("syncedLyrics")
                if sl:
                    _LAST_LYRICS_SOURCE = "LRCLib — exact match (synced)"
                    return sl, False
                pl = entry.get("plainLyrics")
                if pl:
                    plain_fallback = (pl, "LRCLib — exact match (plain)")
        except Exception:
            pass

    # ── Fallback: loosely-ranked search ─────────────────────────────────────
    data = []
    if artist_q and title_q:
        try:
            data = _try("search", {"track_name": title_q, "artist_name": artist_q})
        except Exception:
            data = []

    if not data:
        try:
            data = _try("search", {"q": query})
        except Exception:
            data = []

    # If we know our own duration, put entries close to it (±2s) first, so a
    # same-title-different-song / different-cut entry doesn't win by accident.
    if duration and data:
        close  = [e for e in data if e.get("duration") and abs(e["duration"] - duration) <= 2]
        others = [e for e in data if e not in close]
        data = close + others

    for entry in data:
        sl = entry.get("syncedLyrics")
        if sl:
            _LAST_LYRICS_SOURCE = "LRCLib — search (synced)"
            return sl, False

    if plain_fallback:
        lyr, src = plain_fallback
        _LAST_LYRICS_SOURCE = src
        return lyr, True

    for entry in data:
        pl = entry.get("plainLyrics")
        if pl:
            _LAST_LYRICS_SOURCE = "LRCLib — search (plain)"
            return pl, True

    return None, False

def _azlyrics_search(query):
    """AZLyrics fallback. Always plain text (AZLyrics has no synced/timed
    lyrics), so callers should treat anything from here as plain."""
    global _LAST_LYRICS_SOURCE, _LAST_LYRICS_ERROR
    import urllib.request, re, html as htmllib

    def _az_slug(s):
        s = s.lower().strip()
        if s.startswith("the "):
            s = s[4:]
        return re.sub(r"[^a-z0-9]", "", s)

    parts = query.split(" - ", 1)
    if len(parts) == 2:
        artist_q, title_q = parts[0].strip(), parts[1].strip()
    else:
        words = query.split()
        mid = max(1, len(words) // 2)
        artist_q, title_q = " ".join(words[:mid]), " ".join(words[mid:])

    artist_slug = _az_slug(artist_q)
    title_slug  = _az_slug(title_q)
    if not (artist_slug and title_slug):
        return None

    az_url = f"https://www.azlyrics.com/lyrics/{artist_slug}/{title_slug}.html"
    req = urllib.request.Request(az_url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0 Safari/537.36",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            page = r.read().decode("utf-8", errors="ignore")
    except Exception as e:
        if _is_conn_error(e):
            _LAST_LYRICS_ERROR = True
        return None

    m = re.search(r"<!--.*?Sorry about that\.\s*-->\s*<div>(.*?)</div>", page, re.DOTALL)
    if not m:
        return None
    raw = m.group(1)
    raw = re.sub(r"<br\s*/?>", "\n", raw)
    raw = re.sub(r"<.*?>", "", raw)
    text = htmllib.unescape(raw).strip()
    if text:
        _LAST_LYRICS_SOURCE = "AZLyrics"
        return text
    return None

def _fetch_lyrics(query, prefer_lrclib=False, duration=None):
    """Fetch lyrics from various providers, optionally preferring LRCLib.
    duration (seconds) lets LRCLib return its exact, verified match instead
    of the first loosely-ranked search hit — pass it whenever you have the
    audio file (see _get_duration).

    Tries providers in order but does NOT stop at the first plain-only hit —
    it keeps going in search of a synced result, and only settles for plain
    lyrics (flagged via _LAST_LYRICS_IS_PLAIN) once every provider has been
    exhausted. Sets _LAST_LYRICS_SOURCE / _LAST_LYRICS_IS_PLAIN / 
    _LAST_LYRICS_ERROR as side effects — read them right after calling."""
    global _LAST_LYRICS_SOURCE, _LAST_LYRICS_IS_PLAIN, _LAST_LYRICS_ERROR
    _LAST_LYRICS_SOURCE = None
    _LAST_LYRICS_IS_PLAIN = False
    _LAST_LYRICS_ERROR = False

    order = ("lrclib", "syncedlyrics", "azlyrics") if prefer_lrclib else ("syncedlyrics", "lrclib", "azlyrics")
    plain_fallback = None  # (lyrics, source)

    for prov in order:
        lyr, is_plain, src = None, False, None
        try:
            if prov == "lrclib":
                lyr, is_plain = _lrclib_search(query, duration=duration)
                src = _LAST_LYRICS_SOURCE
            elif prov == "syncedlyrics":
                import syncedlyrics
                lyr = syncedlyrics.search(query)
                # NOTE: syncedlyrics can return plain (non-timed) lyrics from
                # some of its backends -- don't just assume synced, actually
                # check for timestamp tags. This is what was letting plain
                # results slip straight through to the embed prompt.
                is_plain = not _looks_synced(lyr)
                src = "syncedlyrics (aggregator: Musixmatch/NetEase/Genius/etc.)"
            else:  # azlyrics
                lyr = _azlyrics_search(query)
                is_plain = True
                src = _LAST_LYRICS_SOURCE
        except ImportError:
            continue
        except Exception as e:
            if _is_conn_error(e):
                _LAST_LYRICS_ERROR = True
            continue

        if not lyr:
            continue
        if not is_plain:
            _LAST_LYRICS_SOURCE = src
            return lyr
        if plain_fallback is None:
            plain_fallback = (lyr, src)

    if plain_fallback:
        lyr, src = plain_fallback
        _LAST_LYRICS_SOURCE = src
        _LAST_LYRICS_IS_PLAIN = True
        return lyr

    return None

_LYRICS_DECLINED = object()  # sentinel: user explicitly declined the custom-query
                             # fallback (as opposed to trying it and still finding
                             # nothing) -- callers use this to skip the manual
                             # .lrc-editor fallback entirely instead of forcing it.

def _try_search_again_or_custom(query, file_name, prefer_lrclib=False, duration=None, indent=""):
    """Called once an automatic lyrics search comes back empty. If the miss
    was caused by a connection problem, offer to just re-run the SAME search
    first (the query was likely fine — the network just hiccuped) instead of
    immediately asking for a custom query. Falls through to a custom-query
    prompt either way once that's declined or exhausted.
    Returns lyrics (str), None if a custom query was tried but still found
    nothing, or _LYRICS_DECLINED if the user declined the custom-query prompt
    outright."""
    if _LAST_LYRICS_ERROR:
        while True:
            again = input(f"{indent}{Y}[!]{R} Connection issue while searching. "
                          f"Try to search it automatic again? (y/N): ").strip().lower()
            if again != "y":
                break
            print(f"{indent}{C}[~]{R} Retrying: {query} ...")
            lyrics = _fetch_lyrics(query, prefer_lrclib=prefer_lrclib, duration=duration)
            if lyrics:
                return lyrics
            if not _LAST_LYRICS_ERROR:
                break  # now a genuine not-found, drop to the custom-query prompt below
    retry = input(f"{indent}Try custom query? (y/N): ").strip().lower()
    if retry == "y":
        q2 = _ask(f"{indent}Query: ").strip()
        if q2:
            print(f"{indent}{C}[~]{R} Searching: {q2} ...")
            return _fetch_lyrics(q2, prefer_lrclib=prefer_lrclib, duration=duration)
        return None
    return _LYRICS_DECLINED

def _gate_plain_lyrics(lyrics, query, prefer_lrclib=False, duration=None, indent=""):
    """If the fetched lyrics are plain-only (no synced timestamps), don't
    hand them straight to the preview/embed flow — warn, show a preview of
    what was actually found, and ask what to do next. If the plain result
    didn't come from LRCLib itself, also offer to specifically retry with
    LRCLib prioritized (LRCLib's own /api/get exact-match lookup can still
    turn up a synced version that a syncedlyrics/AZLyrics plain hit missed).
    Returns the lyrics to use (str) or None if the user backs out."""
    if not (lyrics and _LAST_LYRICS_IS_PLAIN):
        return lyrics
    src = _LAST_LYRICS_SOURCE
    came_from_lrclib = bool(src) and src.startswith("LRCLib")
    print(f"\n{indent}{Y}[!]{R} Only plain (non-synced) lyrics found, via {src}.")
    _preview_lyrics(lyrics, src)
    if came_from_lrclib:
        prompt = f"{indent}Use it anyway? (y = use / n = skip / c = try a custom query): "
    else:
        prompt = f"{indent}Use it anyway? (y = use / n = skip / c = custom query / l = try LRCLib first): "
    choice = input(prompt).strip().lower()
    if choice == "l" and not came_from_lrclib:
        print(f"{indent}{C}[~]{R} Retrying with LRCLib prioritized: {query} ...")
        new_lyrics = _fetch_lyrics(query, prefer_lrclib=True, duration=duration)
        if not new_lyrics:
            print(f"{indent}{Y}[!]{R} LRCLib (and everything else) came up empty.")
            return _gate_plain_lyrics(lyrics, query, prefer_lrclib=prefer_lrclib, duration=duration, indent=indent)
        return _gate_plain_lyrics(new_lyrics, query, prefer_lrclib=True, duration=duration, indent=indent)
    if choice == "c":
        q2 = _ask(f"{indent}Query: ").strip()
        if q2:
            print(f"{indent}{C}[~]{R} Searching: {q2} ...")
            new_lyrics = _fetch_lyrics(q2, prefer_lrclib=prefer_lrclib, duration=duration)
            return _gate_plain_lyrics(new_lyrics, q2, prefer_lrclib=prefer_lrclib, duration=duration, indent=indent)
        return None
    if choice != "y":
        return None
    return lyrics

def _preview_lyrics(lyrics, source=None):
    """Show first 20 lines of lyrics as preview, plus which provider it came from."""
    if source:
        print(f"\n  {DIM}Source: {source}{R}")
    lines = lyrics.strip().splitlines()
    preview = lines[:20]
    print("\n── Lyrics preview (first 20 lines) ──")
    for line in preview:
        print(f"  {line}")
    if len(lines) > 20:
        print(f"  ... (+{len(lines) - 20} lines)")
    print("──────────────────────────────────────")

def _has_lyrics(fp):
    """Return True if file already has embedded lyrics."""
    ext = Path(fp).suffix.lower()
    try:
        if ext == ".m4a":
            from mutagen.mp4 import MP4
            audio = MP4(str(fp))
            lyr = audio.tags.get("\xa9lyr") if audio.tags else None
            return bool(lyr and str(lyr[0]).strip())
        elif ext in (".flac", ".opus"):
            audio = _open_vorbis(fp, ext)
            lyr = audio.get("lyrics") if audio.tags else None
            return bool(lyr and str(lyr[0]).strip())
        else:
            from mutagen.id3 import ID3
            tags = ID3(str(fp))
            uslt = tags.getall("USLT")
            return any(bool(str(u.text).strip()) for u in uslt)
    except Exception:
        return False

def _get_lyrics(fp):
    """Return lyrics string from file, or None if not present."""
    ext = Path(fp).suffix.lower()
    try:
        if ext == ".m4a":
            from mutagen.mp4 import MP4
            audio = MP4(str(fp))
            lyr = audio.tags.get("\xa9lyr") if audio.tags else None
            if lyr:
                return str(lyr[0]).strip()
        elif ext in (".flac", ".opus"):
            audio = _open_vorbis(fp, ext)
            lyr = audio.get("lyrics") if audio.tags else None
            if lyr:
                return str(lyr[0]).strip()
        else:
            from mutagen.id3 import ID3
            tags = ID3(str(fp))
            uslt = tags.getall("USLT")
            if uslt:
                return str(uslt[0].text).strip()
    except Exception:
        pass
    return None

def _embed_lyrics(fp, lyrics):
    """Embed lyrics string into mp3/m4a/flac/opus file. Always prepends a 'TN' marker as
    the very first line -- before any other content (timestamps, or any
    artist/album-style header a source might include) -- unless it's already
    there. Wrapped as a real LRC timestamp line ("[00:00.00]TN") instead of a
    bare line: strict LRC parsers (some lock-screen/now-playing lyrics widgets)
    reject the ENTIRE lyrics block as invalid if even one line has no
    timestamp, which showed up as "No Lyrics" even though the tag had content.
    This one function is the single funnel for every lyrics-embed path (manual
    edit via lyric_editor, and every auto-fetch mode), so the marker ends up
    applied automatically everywhere without touching each call site.

    Some providers (LRCLib, syncedlyrics) prepend a metadata header block
    before the actual timed lines -- e.g. "[id: ...]", "[ti: ...]",
    "[ar: ...]", "[al: ...]", "[by: ...]"/"[au: ...]", "[length: ...]".
    Those have no mm:ss timestamp, so some lock-screen/now-playing widgets
    render them as literal lyric text instead of skipping them. Strip that
    block before re-stamping "TN".

    A file embedded by an older/broken version may already have "[00:00.00]TN"
    sitting BEFORE that metadata block (instead of after it was stripped) --
    if so, drop that old marker line first so the block underneath still
    gets cleaned out on re-save, instead of being mistaken for "already
    fixed" and left stuck there forever."""
    import re
    lines = lyrics.split("\n")
    if lines and re.match(r"^\[00:00\.00\]\s*TN\s*$", lines[0].strip()):
        lines = lines[1:]
    i = 0
    while i < len(lines) and re.match(r"^\[[A-Za-z]+:", lines[i].strip()):
        i += 1
    lyrics = "\n".join(lines[i:]).lstrip("\n")

    first_line = lyrics.split("\n", 1)[0].strip()
    if "TN" not in first_line:
        lyrics = "[00:00.00]TN\n" + lyrics
    ext = Path(fp).suffix.lower()
    if ext == ".m4a":
        from mutagen.mp4 import MP4
        audio = MP4(str(fp))
        audio["\xa9lyr"] = [lyrics]
        audio.save()
    elif ext in (".flac", ".opus"):
        audio = _open_vorbis(fp, ext)
        audio["lyrics"] = [lyrics]
        audio.save()
    else:
        from mutagen.id3 import ID3, USLT
        from mutagen.mp3 import MP3
        try:
            tags = ID3(str(fp))
        except Exception:
            audio = MP3(str(fp), ID3=ID3)
            audio.add_tags()
            tags = audio.tags
        tags.delall("USLT")
        tags.add(USLT(encoding=3, lang="eng", desc="", text=lyrics))
        tags.save(str(fp))
    _history_log("lyrics", "Lyrics embedded/updated", file=Path(fp).name)

_TN_MARKER_RE = re.compile(r"^\[00:00\.00\]\s*TN\s*$")

def _lyrics_validation_status(fp):
    """Read-only check of a file's embedded lyrics for Validation. Returns
    'missing' if there's no lyrics tag at all, 'placeholder' if the only
    content is the 'TN' marker _embed_lyrics() stamps when nothing was
    found/entered, or None if real lyrics are present."""
    if not _has_lyrics(fp):
        return "missing"
    lyrics = _get_lyrics(fp) or ""
    lines = lyrics.split("\n")
    if lines and _TN_MARKER_RE.match(lines[0].strip()):
        lines = lines[1:]
    if not any(l.strip() for l in lines):
        return "placeholder"
    return None

# ─── lyrics menu ──────────────────────────────────────────────────────────────

def _manual_lyrics_fallback(fp, indent=""):
    """Called once every automatic source has been exhausted and no lyrics
    were found for `fp`. Instead of silently embedding a placeholder tag,
    touch an empty .lrc next to the file and open it in the lyrics editor
    (msedit/nano) so the user can type or paste lyrics by hand. Whatever
    is saved gets embedded; if the file is left empty, falls back to the
    'TN' placeholder so players don't show 'No Lyrics'."""
    editor = _pick_lyrics_editor()
    lrc_path = Path(Path(fp).stem + ".lrc")
    if not lrc_path.exists():
        lrc_path.touch()
    print(f"{indent}{DIM}[~] No lyrics found. Created {lrc_path.name}, opening {editor}...{R}")
    while True:
        subprocess.run([editor, str(lrc_path)])
        selesai = input(f"{indent}Done editing? (Y/n): ").strip().lower()
        if selesai == "n":
            print(f"{indent}Re-opening {editor}...")
            continue
        elif selesai == "y":
            break
        else:
            print(f"{indent}Unrecognized answer, type Y or N.")
            continue
    try:
        with open(lrc_path, "r", encoding="utf-8") as f:
            new_lyrics = f.read()
    except Exception as e:
        print(f"{indent}{Y}[!]{R} Failed to read {lrc_path.name}: {e}")
        new_lyrics = ""
    if new_lyrics.strip():
        _embed_lyrics(fp, new_lyrics)
        print(f"{indent}{G}[+]{R} Lyrics embedded to {Path(fp).name}!")
    else:
        _embed_lyrics(fp, "")
        print(f"{indent}{DIM}[~] Left empty — placeholder 'TN' embedded so the player doesn't show 'No Lyrics'.{R}")
    try:
        lrc_path.unlink()
        print(f"{indent}{DIM}[—] {lrc_path.name} deleted.{R}")
    except Exception:
        pass

def _ask_custom_query_and_fetch(current_query, file_name, prefer_lrclib=False, duration=None):
    retry = input(f"  Try custom query for {file_name}? (y/N): ").strip().lower()
    if retry == "y":
        q2 = _ask("  Query: ").strip()
        if q2:
            print(f"  {C}[~]{R} Searching: {q2} ...")
            lyrics = _fetch_lyrics(q2, prefer_lrclib=prefer_lrclib, duration=duration)
            return _gate_plain_lyrics(lyrics, q2, prefer_lrclib=prefer_lrclib, duration=duration, indent="  ")
    return None

def _fetch_lyrics_for_file(fp, prefer_lrclib=False):
    """Single-file interactive auto-fetch: search, preview, confirm, embed --
    falling back to manual entry if nothing turns up. This is the exact flow
    the Lyrics menu's 'auto fetch single' mode uses, factored out here so
    Validation can offer the same experience for files it flags as missing/
    placeholder-only lyrics."""
    title, artist = _get_tags(fp)
    tags_empty = not (artist or title)
    if not title:
        title = fp.stem  # fallback to filename when title tag is missing
    query = f"{artist} - {title}" if artist else title
    print(f"\n  {DIM}Query: {query}{R}")
    if tags_empty:
        ovr = input("  Tags empty, using filename. Override? (y/N): ").strip().lower()
        if ovr == "y":
            query = _ask("  Manual query: ").strip() or query
    if not query:
        print("Skipped.")
        return
    fdur = _get_duration(fp)
    print(f"{_ts()} {C}[~]{R} Searching: {query} ...")
    lyrics = _fetch_lyrics(query, prefer_lrclib=prefer_lrclib, duration=fdur)
    if not lyrics:
        print(f"{Y}[!]{R} Lyrics not found.")
        lyrics = _try_search_again_or_custom(query, fp.name, prefer_lrclib=prefer_lrclib, duration=fdur, indent="  ")
        if lyrics is _LYRICS_DECLINED:
            print(f"{DIM}[~] Skipped — no custom query.{R}")
            return
    lyrics = _gate_plain_lyrics(lyrics, query, prefer_lrclib=prefer_lrclib, duration=fdur, indent="  ")
    if not lyrics:
        print(f"{Y}[!]{R} Still not found. {_ts()}")
        _manual_lyrics_fallback(fp)
        return
    _preview_lyrics(lyrics, _LAST_LYRICS_SOURCE)
    konfirm = input("\nEmbed these lyrics? (Y/n): ").strip().lower()
    if konfirm == "n":
        custom = _ask_custom_query_and_fetch(query, fp.name, prefer_lrclib=prefer_lrclib, duration=fdur)
        if custom:
            lyrics = custom
            _preview_lyrics(lyrics, _LAST_LYRICS_SOURCE)
            konfirm = input("\nEmbed these lyrics? (Y/n): ").strip().lower()
            if konfirm != "n":
                _embed_lyrics(fp, lyrics)
                print(f"{G}[+]{R} Lyrics embedded to {fp.name}! {_ts()}")
                return
        print("Skipped.")
        return
    _embed_lyrics(fp, lyrics)
    print(f"{G}[+]{R} Lyrics embedded to {fp.name}! {_ts()}")


def add_lyrics(fp=None):
    print("\nLyrics mode:")
    print("  1. Auto fetch (syncedlyrics + LRCLib)")
    print("  2. Load from .lrc / .txt file")
    print("  3. Batch fetch all files in this folder")
    print("  4. Edit lyrics (via msedit / nano)")
    print("  0. Back")
    try:
        mode = int(_ask("\nPick: "))
    except ValueError:
        print("Cancelled.")
        return

    if mode == 0:
        return

    # ── mode 4: manual lyrics editor ───────────────────────────────
    if mode == 4:
        lyric_editor()
        return

    # ── mode 3: batch ──────────────────────────────────────────────
    if mode == 3:
        files = scan_audio()
        if not files:
            print(f"\n{Y}[!]{R} {_scan_empty_message()}")
            return

        _sub_box("Batch Lyrics", [
            (1, "Auto",         "all files in folder"),
            (2, "Manual input", "select specific files"),
            (0, "Back",         None),
        ], width=50, icon="♪")
        try:
            sub_mode = int(_ask(f"\n{V}  ❯{R} Pick: "))
        except ValueError:
            sub_mode = 0
        if sub_mode == 0:
            return
        if sub_mode not in (1, 2):
            print(f"  {Y}[!]{R} Invalid choice.")
            return

        if sub_mode == 2:
            items = _sorted_song_picks(files)
            files = [it[0] for it in items]
            rows = _song_pick_rows(items)
            rows.append((f"   0. Back", f"   {P}0{R}. {DIM}Back{R}"))
            _box("Pick File(s) (multi)", rows, min_width=50, icon="☰")
            raw = _ask(f"\n{V}  ❯{R} Number(s) (e.g., 1,4,5): ").strip()
            if raw == "0" or not raw:
                return
            picks = _parse_multi_input(raw, len(files))
            if not picks:
                print(f"  {Y}[!]{R} No valid numbers.")
                return
            files = [files[i-1] for i in picks]

        _sub_box("Provider Priority", [
            (1, "Normal",              "syncedlyrics first"),
            (2, "Prioritize LRCLib",   "LRCLib first"),
        ], width=50, icon="♪")
        try:
            provider_choice = int(_ask(f"\n{V}  ❯{R} Pick (1/2): "))
        except ValueError:
            provider_choice = 1
        prefer_lrclib = (provider_choice == 2)

        print(f"\n{_ts()} {LV}[i]{R} Found {len(files)} file(s). Starting batch fetch...\n")
        _print_patience_banner()
        failed = []
        skipped_has_lyrics = []
        for i, bfp in enumerate(files, 1):
            title, artist = _get_tags(bfp)
            if not title:
                title = bfp.stem  # fallback to filename when title tag is missing
            query = f"{artist} - {title}" if artist else title
            print(f"[{i}/{len(files)}] {bfp.name}")
            if sub_mode == 1 and _has_lyrics(bfp):
                print(f"      {DIM}[—] Already has lyrics, skipped.{R}\n")
                skipped_has_lyrics.append(bfp.name)
                continue
            print(f"      Query: {query}")
            bdur = _get_duration(bfp)

            lyrics = _fetch_lyrics(query, prefer_lrclib=prefer_lrclib, duration=bdur)
            lyrics = _gate_plain_lyrics(lyrics, query, prefer_lrclib=prefer_lrclib, duration=bdur, indent="      ")
            lyr_src = _LAST_LYRICS_SOURCE

            if lyrics:
                _preview_lyrics(lyrics, lyr_src)
                konfirm = input("      Embed? (Y/n): ").strip().lower()
                if konfirm == "n":
                    custom = _ask_custom_query_and_fetch(query, bfp.name, prefer_lrclib=prefer_lrclib, duration=bdur)
                    if custom:
                        lyrics = custom
                        _preview_lyrics(lyrics, _LAST_LYRICS_SOURCE)
                        konfirm = input("      Embed? (Y/n): ").strip().lower()
                        if konfirm != "n":
                            _embed_lyrics(bfp, lyrics)
                            print(f"      {G}[+]{R} Done! {_ts()}\n")
                            continue
                    failed.append((bfp.name, "skipped by user"))
                    print()
                    continue
                if konfirm != "n":
                    _embed_lyrics(bfp, lyrics)
                    print(f"      {G}[+]{R} Done! {_ts()}\n")
                else:
                    print(f"      {C}[~]{R} Skipped.\n")
                    failed.append((bfp.name, "skipped by user"))
            else:
                print(f"      {Y}[!]{R} Not found automatically.")
                lyrics = _try_search_again_or_custom(query, bfp.name, prefer_lrclib=prefer_lrclib, duration=bdur, indent="      ")
                if lyrics is _LYRICS_DECLINED:
                    print(f"      {DIM}[~] Skipped — no custom query.{R}\n")
                    failed.append((bfp.name, "skipped by user"))
                    continue
                lyrics = _gate_plain_lyrics(lyrics, query, prefer_lrclib=prefer_lrclib, duration=bdur, indent="      ")
                if lyrics:
                    _preview_lyrics(lyrics, _LAST_LYRICS_SOURCE)
                    konfirm = input("      Embed? (Y/n): ").strip().lower()
                    if konfirm != "n":
                        _embed_lyrics(bfp, lyrics)
                        print(f"      {G}[+]{R} Done! {_ts()}\n")
                        continue
                failed.append((bfp.name, "lyrics not found — sent to manual edit"))
                _manual_lyrics_fallback(bfp, indent="      ")

        if skipped_has_lyrics:
            print(f"\n{DIM}[—] {len(skipped_has_lyrics)} file(s) skipped (already have lyrics).{R}")
        if failed:
            print("\n── Failed/skipped ──")
            for name, reason in failed:
                print(f"  ✗ {name}  ({reason})")
            print(f"\n{_ts()} {DIM}Batch finished.{R}")
        else:
            print(f"\n{G}[+]{R} All files processed successfully! {_ts()}")
        return

    # ── mode 1: auto fetch single ───────────────────────────────────
    if mode == 1:
        fps = pick_files()
        if not fps:
            return
        _sub_box("Provider Priority", [
            (1, "Normal",              "syncedlyrics first"),
            (2, "Prioritize LRCLib",   "LRCLib first"),
        ], width=50, icon="♪")
        try:
            provider_choice = int(_ask(f"\n{V}  ❯{R} Pick (1/2): "))
        except ValueError:
            provider_choice = 1
        prefer_lrclib = (provider_choice == 2)

        _print_patience_banner()
        for fp in fps:
            _fetch_lyrics_for_file(fp, prefer_lrclib=prefer_lrclib)
        return

    # ── mode 2: load .lrc/.txt files ─────────────────────────────────
    elif mode == 2:
        lrc_files = []
        for lext in ("*.lrc", "*.txt", "*.LRC", "*.TXT"):
            lrc_files.extend(sorted(Path(".").glob(lext)))

        if not lrc_files:
            print(f"\n{Y}[!]{R} No .lrc/.txt files in this folder.")
            return

        audio_files = scan_audio()
        if not audio_files:
            print(f"\n{Y}[!]{R} {_scan_empty_message('No audio files in this folder.')}")
            return

        def extract_lrc_title(lrc_path):
            """Return the [ti:] metadata tag if present, otherwise fall back to
            the file's own name (NOT a lyric line — an arbitrary lyric line
            has no relation to the song title and only pollutes the fuzzy
            token matcher with unrelated words)."""
            import re
            try:
                with open(lrc_path, "r", encoding="utf-8", errors="ignore") as f:
                    lines = [f.readline().strip() for _ in range(10)]
            except Exception:
                return Path(lrc_path).stem
            for line in lines:
                m = re.match(r"\[ti:(.+?)\]", line, re.IGNORECASE)
                if m:
                    return m.group(1).strip()
            return Path(lrc_path).stem

        print(f"\n{LV}[i]{R} {len(lrc_files)} lyric file(s), {len(audio_files)} song(s) found.")
        print(f"{LV}[i]{R} Matching...\n")

        matched, unmatched_lrc = [], []
        audio_stem_map = {afp.stem.lower(): afp for afp in audio_files}

        for lrc in lrc_files:
            lrc_title = extract_lrc_title(lrc)
            lrc_tokens = _tokenize(lrc.stem)
            if lrc_title:
                lrc_tokens |= _tokenize(lrc_title)

            best_audio, best_score = None, 0.0
            lrc_stem_lower = lrc.stem.lower()
            if lrc_stem_lower in audio_stem_map:
                best_audio = audio_stem_map[lrc_stem_lower]
                best_score = 1.0
            else:
                for afp in audio_files:
                    title, _ = _get_tags(afp)
                    audio_tokens = _tokenize(title if title else afp.stem)
                    if not lrc_tokens or not audio_tokens:
                        continue
                    s1 = _fuzzy_token_score(lrc_tokens, audio_tokens) / len(lrc_tokens)
                    s2 = _fuzzy_token_score(audio_tokens, lrc_tokens) / len(audio_tokens)
                    score = (2 * s1 * s2 / (s1 + s2)) if (s1 + s2) > 0 else 0
                    if score > best_score:
                        best_score, best_audio = score, afp

            if best_audio and best_score >= 0.4:
                matched.append((lrc, best_audio, round(best_score, 2), lrc_title))
            else:
                unmatched_lrc.append(lrc)

        if matched:
            print("── Match results ──")
            for lrc, afp, score, lrc_title in matched:
                print(f"  ✓ {lrc.name}")
                if lrc_title:
                    print(f"      LRC title : {lrc_title}")
                print(f"      → {afp.name}  ({score:.0%} match)")
        if unmatched_lrc:
            print("\n── No match ──")
            for lrc in unmatched_lrc:
                print(f"  ✗ {lrc.name}")

        if not matched:
            print(f"\n{Y}[!]{R} No matching pairs.")
            return

        print()
        for lrc, afp, _, lrc_title in matched:
            konfirm = input(f"Embed [{lrc.name}] → [{afp.name}]? (Y/n): ").strip().lower()
            if konfirm == "n":
                print(f"  {C}[~]{R} Skipped.\n")
                continue
            try:
                with open(lrc, "r", encoding="utf-8", errors="ignore") as f:
                    lyr = f.read()
                _embed_lyrics(afp, lyr)
                print(f"  {G}[+]{R} Done!")
                try:
                    lrc.unlink()
                    print(f"  {DIM}[—] {lrc.name} deleted.{R}\n")
                except Exception:
                    print()
            except Exception as e:
                print(f"  {Y}[!]{R} Failed: {e}\n")
        return

    else:
        print(f"{Y}[!]{R} Invalid choice.")
        return

def _pick_lyrics_editor():
    """Prefer msedit if it's installed, fall back to nano otherwise."""
    import shutil as _shutil
    if _shutil.which("msedit"):
        return "msedit"
    return "nano"

def lyric_editor():
    """Edit lyrics of selected audio file(s) using msedit (if installed) or nano."""
    editor = _pick_lyrics_editor()
    fps = pick_files()
    if not fps:
        return
    for afp in fps:
        lyrics = _get_lyrics(afp)
        if lyrics is None:
            print(f"  {Y}[!]{R} No lyrics in {afp.name}.")
            _manual_lyrics_fallback(afp, indent="  ")
            continue
        lrc_path = Path(afp.stem + ".lrc")
        with open(lrc_path, "w", encoding="utf-8") as f:
            f.write(lyrics)
        print(f"  {G}[+]{R} Lyrics extracted to {lrc_path.name}, opening {editor}...")
        while True:
            subprocess.run([editor, str(lrc_path)])
            selesai = input("  Done editing? (Y/n): ").strip().lower()
            if selesai == "n":
                print(f"  Re-opening {editor}...")
                continue
            elif selesai == "y":
                break
            else:
                print("  Unrecognized answer, type Y or N.")
                continue
        try:
            with open(lrc_path, "r", encoding="utf-8") as f:
                new_lyrics = f.read()
        except Exception as e:
            print(f"  {Y}[!]{R} Failed to read {lrc_path.name}: {e}")
            continue
        if not new_lyrics.strip():
            print(f"  {Y}[!]{R} Lyrics empty, not embedding.")
            continue
        try:
            _embed_lyrics(afp, new_lyrics)
            print(f"  {G}[+]{R} Lyrics embedded to {afp.name}!")
            try:
                lrc_path.unlink()
                print(f"  {DIM}[—] {lrc_path.name} deleted.{R}")
            except Exception as e:
                print(f"  {Y}[!]{R} Failed to delete {lrc_path.name}: {e}")
        except Exception as e:
            print(f"  {Y}[!]{R} Failed to embed lyrics: {e}")

def tags_editor():
    """Sub-menu: edit title / artist / album / composer / genre / year / track /
    disc. Lyrics editing lives in the Lyrics menu instead. Copyright editing
    stays exclusive to the 33333 secret menu -- not exposed here."""
    NEW_FIELDS = {
        4: ("composer", "Composer"),
        5: ("genre",    "Genre"),
        6: ("year",     "Year"),
        7: ("track",    "Track Number"),
        8: ("disc",     "Disc Number"),
    }
    while True:
        _sub_box("Edit Tags", [
            (1, "Change Title",   "edit song title"),
            (2, "Change Artist",  "single / batch"),
            (3, "Change Album",   "single / batch"),
            (4, "Change Composer","single / batch"),
            (5, "Change Genre",   "single / batch"),
            (6, "Change Year",    "single / batch"),
            (7, "Change Track #", "single / batch, e.g. 3 or 3/12"),
            (8, "Change Disc #",  "single / batch, e.g. 1 or 1/2"),
            (0, "Back",           None),
        ], width=58, icon="✎")
        try:
            sub = int(_ask(f"\n{V}  ❯{R} Pick: "))
        except (ValueError, EOFError):
            continue

        if sub == 0:
            return

        if sub == 1:
            fps = pick_files()
            for fp in fps:
                set_tag(fp, "title")

        elif sub == 2:
            tag_t = "artist"
            lbl   = "Artist"
            _sub_box(f"Change {lbl}", [
                (1, "Single", "one file"),
                (2, "Batch",  "auto-detect / multi file"),
                (0, "Back",   None),
            ], width=40, icon="✎")
            try:
                mode = int(_ask(f"\n{V}  ❯{R} Pick: "))
            except (ValueError, EOFError):
                continue
            if mode == 1:
                fps = pick_files()
                for fp in fps:
                    set_tag(fp, "artist")
            elif mode == 2:
                batch_set_tag("artist")

        elif sub == 3:
            tag_t = "album"
            lbl   = "Album"
            _sub_box(f"Change {lbl}", [
                (1, "Single", "one file"),
                (2, "Batch",  "auto-detect / multi file"),
                (0, "Back",   None),
            ], width=40, icon="✎")
            try:
                mode = int(_ask(f"\n{V}  ❯{R} Pick: "))
            except (ValueError, EOFError):
                continue
            if mode == 1:
                fps = pick_files()
                for fp in fps:
                    set_tag(fp, "album")
            elif mode == 2:
                batch_set_tag("album")

        elif sub in NEW_FIELDS:
            tag_t, lbl = NEW_FIELDS[sub]
            _sub_box(f"Change {lbl}", [
                (1, "Single", "one file"),
                (2, "Batch",  "multi file, one value"),
                (0, "Back",   None),
            ], width=40, icon="✎")
            try:
                mode = int(_ask(f"\n{V}  ❯{R} Pick: "))
            except (ValueError, EOFError):
                continue
            if mode == 1:
                fps = pick_files()
                for fp in fps:
                    set_tag(fp, tag_t)
            elif mode == 2:
                _batch_set_simple(tag_t, lbl)

        else:
            print(f"  {Y}[!]{R} Invalid choice.")

# ─── main menu ────────────────────────────────────────────────────────────────

MENU = [
    ("Extract cover art",      "Save cover image to .jpg file",              "◇"),
    ("Insert cover art",       "Embed image into song (manual / batch)",     "◆"),
    ("Check tags",             "Show all metadata",                          "☰"),
    ("Edit Tags",              "Change title / artist / album (single/batch)","✎"),
    ("Lyrics",                 "Fetch, embed & edit lyrics (auto / file / batch)", "♪"),
    ("Export/Import Metadata", "Save or load tags + cover to/from file",     "⇄"),
    ("Sorcerer",               "Search & download songs from YouTube Music", "✧"),
    ("Validation",             "Check Artist tags & lyrics for issues",      "✓"),
    ("Settings",               "App preferences & defaults",                 "⚙"),
]

def _print_menu():
    w = 56
    print()
    print(f"{V}  ╔{'═' * w}╗{R}")
    print(f"{V}  ║{P}{'✦':^{w}}{V}║{R}")
    print(f"{V}  ║{LV}{'T A G M A N':^{w}}{V}║{R}")
    print(f"{V}  ║{DIM}{'Created by Gya & the Companions':^{w}}{V}║{R}")
    print(f"{V}  ║{DIM}{ENV_LABEL:^{w}}{V}║{R}")
    print(f"{V}  ╠{'═' * w}╣{R}")
    for i, (name, desc, icon) in enumerate(MENU, 1):
        plain1 = f"  {icon} {i}. {name}"
        plain2 = f"       {desc}"
        pad1 = max(0, w - len(plain1))
        pad2 = max(0, w - len(plain2))
        print(f"{V}  ║{R}  {P}{icon}{R} {P}{i}{V}.{R} {LV}{name}{R}{' ' * pad1}{V}║{R}")
        print(f"{V}  ║{R}       {DIM}{desc}{R}{' ' * pad2}{V}║{R}")
        if i < len(MENU):
            print(f"{V}  ║{DIM}  {'·' * (w-2)}{V}║{R}")
    print(f"{V}  ╠{'═' * w}╣{R}")
    plain0 = "  ✕ 0. Exit"
    pad0 = max(0, w - len(plain0))
    print(f"{V}  ║{R}  {P}✕{R} {P}0{V}.{R} {DIM}Exit{R}{' ' * pad0}{V}║{R}")
    print(f"{V}  ╚{'═' * w}╝{R}")
    print(f"{DIM}{'✦ Manager for your music, through your terminal ✦':^{w+4}}{R}")



# ─── metadata export / import ─────────────────────────────────────────────────

def _pick_list(items, label, display_fn):
    if not items:
        return None
    rows = []
    for i, item in enumerate(items, 1):
        text = display_fn(item)
        rows.append((f"  {i}. {text}", f"  {P}{i}{R}. {LV}{text}{R}"))
    rows.append((f"  0. Back", f"  {P}0{R}. {DIM}Back{R}"))
    _box(label, rows, min_width=42, icon="☰")
    try:
        pick = int(_ask(f"\n{V}  ❯{R} Number: "))
    except ValueError:
        return None
    if pick == 0 or not (1 <= pick <= len(items)):
        return None
    return items[pick - 1]


def meta_extract(fp):
    import json as jsonlib
    ext  = Path(fp).suffix.lower()
    stem = Path(fp).stem
    meta = {"source_file": fp.name}

    if ext == ".m4a":
        from mutagen.mp4 import MP4
        audio = MP4(str(fp))
        tag_map = {
            "\xa9nam": "title",    "\xa9ART": "artist",   "\xa9alb": "album",
            "\xa9day": "year",     "\xa9gen": "genre",    "aART":    "album_artist",
            "\xa9wrt": "composer", "\xa9cmt": "comment",  "\xa9lyr": "lyrics",
            "trkn":    "track",    "disk":    "disc",      "tmpo":    "bpm",
        }
        if audio.tags:
            for key, field in tag_map.items():
                val = audio.tags.get(key)
                if val:
                    v = val[0]
                    meta[field] = str(v[0] if isinstance(v, tuple) else v)
            covr = audio.tags.get("covr")
            if covr:
                cover_path = Path(stem + "_cover.jpg")
                with open(cover_path, "wb") as f:
                    f.write(bytes(covr[0]))
                print(f"  {G}[+]{R} Cover → {cover_path.name}")
    elif ext == ".flac":
        from mutagen.flac import FLAC
        audio = FLAC(str(fp))
        tag_map = {
            "title": "title", "artist": "artist", "album": "album",
            "date": "year", "genre": "genre", "albumartist": "album_artist",
            "composer": "composer", "comment": "comment", "lyrics": "lyrics",
            "tracknumber": "track", "discnumber": "disc", "bpm": "bpm",
        }
        if audio.tags:
            for key, field in tag_map.items():
                val = audio.get(key)
                if val:
                    meta[field] = str(val[0])
        if audio.pictures:
            cover_path = Path(stem + "_cover.jpg")
            with open(cover_path, "wb") as f:
                f.write(audio.pictures[0].data)
            print(f"  {G}[+]{R} Cover → {cover_path.name}")
    elif ext == ".opus":
        audio = _open_vorbis(fp, ext)
        tag_map = {
            "title": "title", "artist": "artist", "album": "album",
            "date": "year", "genre": "genre", "albumartist": "album_artist",
            "composer": "composer", "comment": "comment", "lyrics": "lyrics",
            "tracknumber": "track", "discnumber": "disc", "bpm": "bpm",
        }
        if audio.tags:
            for key, field in tag_map.items():
                val = audio.get(key)
                if val:
                    meta[field] = str(val[0])
        pics = _opus_pictures(audio)
        if pics:
            cover_path = Path(stem + "_cover.jpg")
            with open(cover_path, "wb") as f:
                f.write(pics[0].data)
            print(f"  {G}[+]{R} Cover → {cover_path.name}")
    else:
        from mutagen.id3 import ID3, APIC
        try:
            tags = ID3(str(fp))
        except Exception:
            print(f"  {Y}[!]{R} No ID3 tags.")
            return
        tag_map = {
            "TIT2": "title",  "TPE1": "artist",       "TALB": "album",
            "TDRC": "year",   "TCON": "genre",         "TRCK": "track",
            "TPE2": "album_artist", "TCOM": "composer", "TBPM": "bpm",
            "TPOS": "disc",
        }
        for key, field in tag_map.items():
            val = tags.get(key)
            if val:
                meta[field] = str(val)
        uslt = tags.getall("USLT")
        if uslt:
            meta["lyrics"] = str(uslt[0].text)
        comm = tags.get("COMM::eng") or tags.get("COMM::")
        if comm:
            meta["comment"] = str(comm.text[0]) if comm.text else ""
        apic = tags.get("APIC:") or tags.get("APIC:Cover")
        if apic:
            cover_path = Path(stem + "_cover.jpg")
            with open(cover_path, "wb") as f:
                f.write(apic.data)
            print(f"  {G}[+]{R} Cover → {cover_path.name}")

    json_path = Path(stem + "_meta.json")
    with open(json_path, "w", encoding="utf-8") as f:
        jsonlib.dump(meta, f, ensure_ascii=False, indent=2)
    fields = [k for k in meta if k != "source_file"]
    print(f"  {G}[+]{R} Metadata → {json_path.name}")
    print(f"  {DIM}Fields: {', '.join(fields)}{R}")


def _apply_meta_from_json(fp, json_path):
    import json as jsonlib
    with open(json_path, "r", encoding="utf-8") as f:
        meta = jsonlib.load(f)

    ext = Path(fp).suffix.lower()
    if ext == ".m4a":
        from mutagen.mp4 import MP4
        audio = MP4(str(fp))
        tag_map = {
            "title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb",
            "year":  "\xa9day", "genre":  "\xa9gen", "album_artist": "aART",
            "composer": "\xa9wrt", "comment": "\xa9cmt", "lyrics": "\xa9lyr",
        }
        for field, key in tag_map.items():
            if field in meta:
                audio[key] = [meta[field]]
        if "track" in meta:
            try:
                n = int(str(meta["track"]).split("/")[0])
                audio["trkn"] = [(n, 0)]
            except (ValueError, TypeError):
                pass
        if "bpm" in meta:
            try:
                audio["tmpo"] = [int(float(meta["bpm"]))]
            except (ValueError, TypeError):
                pass
        audio.save()
    elif ext in (".flac", ".opus"):
        audio = _open_vorbis(fp, ext)
        tag_map = {
            "title": "title", "artist": "artist", "album": "album",
            "year": "date", "genre": "genre", "album_artist": "albumartist",
            "composer": "composer", "comment": "comment", "lyrics": "lyrics",
        }
        for field, key in tag_map.items():
            if field in meta:
                audio[key] = [str(meta[field])]
        if "track" in meta:
            audio["tracknumber"] = [str(meta["track"])]
        if "disc" in meta:
            audio["discnumber"] = [str(meta["disc"])]
        if "bpm" in meta:
            try:
                audio["bpm"] = [str(int(float(meta["bpm"])))]
            except (ValueError, TypeError):
                pass
        audio.save()
    else:
        from mutagen.mp3 import MP3
        audio = MP3(str(fp), ID3=ID3)
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags
        cls_map = {
            "title": ("TIT2", TIT2), "artist": ("TPE1", TPE1),
            "album": ("TALB", TALB), "year":   ("TDRC", TDRC),
            "genre": ("TCON", TCON), "track":  ("TRCK", TRCK),
            "album_artist": ("TPE2", TPE2), "composer": ("TCOM", TCOM),
            "bpm":  ("TBPM", TBPM), "disc":   ("TPOS", TPOS),
        }
        for field, (key, cls) in cls_map.items():
            if field in meta:
                tags[key] = cls(encoding=3, text=str(meta[field]))
        if "comment" in meta:
            tags["COMM::eng"] = COMM(encoding=3, lang="eng", desc="", text=meta["comment"])
        if "lyrics" in meta:
            tags.delall("USLT")
            tags.add(USLT(encoding=3, lang="eng", desc="", text=meta["lyrics"]))
        tags.save(str(fp))

def meta_load():
    _sub_box("Load Metadata", [
        (1, "Metadata only",              "all tags from .json → song"),
        (2, "Cover only",                 "pick .jpg → pick song"),
        (3, "All",                        "auto-match json+cover → song"),
        (4, "Lyrics only",                "take lyrics from .json → song"),
        (5, "Metadata (without lyrics)",  "tags except lyrics → song"),
        (0, "Back",                       None),
    ], width=50, icon="⇄")
    try:
        sub = int(_ask(f"\n{V}  ❯{R} Pick: "))
    except ValueError:
        return
    if sub == 0:
        return

    elif sub == 1:
        json_files = sorted(Path(".").glob("*_meta.json"))
        jf = _pick_list(json_files, "Pick JSON", lambda x: x.name)
        if jf is None:
            return
        fps = pick_files()
        for fp in fps:
            try:
                _apply_meta_from_json(fp, jf)
            except Exception as e:
                print(f"  {Y}[!]{R} Failed: {e}")

    elif sub == 2:
        img_files = []
        for iext in ("*.jpg", "*.jpeg", "*.png", "*.JPG", "*.JPEG", "*.PNG"):
            img_files.extend(sorted(Path(".").glob(iext)))
        img = _pick_list(img_files, "Pick Cover",
                         lambda x: f"{x.name}  ({x.stat().st_size // 1024} KB)")
        if img is None:
            return
        fps = pick_files()
        for fp in fps:
            try:
                _embed_cover(fp, img)
                print(f"  {G}[+]{R} Cover imported to {fp.name}!")
            except Exception as e:
                print(f"  {Y}[!]{R} Failed: {e}")

    elif sub == 3:
        import json as jsonlib
        json_files = sorted(Path(".").glob("*_meta.json"))
        if not json_files:
            print(f"\n  {Y}[!]{R} No *_meta.json in this folder.")
            return
        audio_files = scan_audio()
        if not audio_files:
            print(f"\n  {Y}[!]{R} {_scan_empty_message('No audio files in this folder.')}")
            return

        print(f"\n  {LV}[i]{R} Matching {len(json_files)} metadata → {len(audio_files)} songs...\n")
        matched, unmatched = [], []

        for jf in json_files:
            try:
                with open(jf, "r", encoding="utf-8") as f:
                    meta = jsonlib.load(f)
            except Exception as e:
                print(f"  {Y}[!]{R} Failed to read {jf.name}: {e}")
                continue

            json_title  = meta.get("title", "")
            json_tokens = _tokenize(json_title) | _tokenize(jf.stem.replace("_meta", ""))

            best_audio, best_score = None, 0.0
            for afp in audio_files:
                title, _ = _get_tags(afp)
                audio_tokens = _tokenize(title if title else afp.stem)
                if not json_tokens or not audio_tokens:
                    continue
                s1 = _fuzzy_token_score(json_tokens, audio_tokens) / len(json_tokens)
                s2 = _fuzzy_token_score(audio_tokens, json_tokens) / len(audio_tokens)
                score = (2 * s1 * s2 / (s1 + s2)) if (s1 + s2) > 0 else 0
                if score > best_score:
                    best_score, best_audio = score, afp

            base_stem   = jf.stem.replace("_meta", "")
            cover_cands = list(Path(".").glob(f"{base_stem}_cover.*"))
            cover_file  = cover_cands[0] if cover_cands else None

            if best_audio and best_score >= 0.4:
                matched.append((jf, meta, best_audio, cover_file, round(best_score, 2), json_title))
            else:
                unmatched.append(jf)

        if matched:
            print("── Match results ──")
            for jf, meta, afp, cover, score, title in matched:
                audio_title, _ = _get_tags(afp)
                print(f"  ✓ {jf.name}")
                print(f"      JSON title : {title}")
                print(f"      → {afp.name}  (title tag: \"{audio_title}\", score: {score})")
                print(f"      Cover      : {cover.name if cover else '—'}")
        if unmatched:
            print("\n── No match ──")
            for jf in unmatched:
                print(f"  ✗ {jf.name}")
        if not matched:
            print(f"\n  {Y}[!]{R} No matching pairs.")
            return

        print()
        for jf, meta, afp, cover, score, title in matched:
            cover_info = f" + {cover.name}" if cover else ""
            konfirm = input(f"Import [{jf.name}{cover_info}] → [{afp.name}]? (Y/n): ").strip().lower()
            if konfirm == "n":
                print(f"  {C}[~]{R} Skipped.\n")
                continue
            try:
                _apply_meta_from_json(afp, jf)
                if cover:
                    _embed_cover(afp, cover)
                    print(f"  {G}[+]{R} Cover imported!")
                print(f"  {G}[+]{R} Done!\n")
            except Exception as e:
                print(f"  {Y}[!]{R} Failed: {e}\n")
    elif sub == 4:
        import json as jsonlib
        json_files = sorted(Path(".").glob("*_meta.json"))
        jf = _pick_list(json_files, "Pick JSON", lambda x: x.name)
        if jf is None:
            return
        try:
            with open(jf, "r", encoding="utf-8") as f:
                meta = jsonlib.load(f)
        except Exception as e:
            print(f"  {Y}[!]{R} Failed to read JSON: {e}")
            return
        lyrics = meta.get("lyrics", "").strip()
        if not lyrics:
            print(f"  {Y}[!]{R} No \"lyrics\" field in this JSON.")
            return
        fps = pick_files()
        for fp in fps:
            _preview_lyrics(lyrics)
            konfirm = input(f"\nEmbed these lyrics to {fp.name}? (Y/n): ").strip().lower()
            if konfirm == "n":
                print(f"  {C}[~]{R} Skipped.")
                continue
            try:
                _embed_lyrics(fp, lyrics)
                print(f"  {G}[+]{R} Lyrics imported to {fp.name}!")
            except Exception as e:
                print(f"  {Y}[!]{R} Failed: {e}")
    elif sub == 5:
        import json as jsonlib
        json_files = sorted(Path(".").glob("*_meta.json"))
        jf = _pick_list(json_files, "Pick JSON", lambda x: x.name)
        if jf is None:
            return
        try:
            with open(jf, "r", encoding="utf-8") as f:
                meta = jsonlib.load(f)
        except Exception as e:
            print(f"  {Y}[!]{R} Failed to read JSON: {e}")
            return
        fps = pick_files()
        for fp in fps:
            SKIP = {"lyrics", "cover_art"}
            ext = fp.suffix.lower()
            if ext == ".m4a":
                from mutagen.mp4 import MP4
                M4A_MAP = {"title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb",
                           "year": "\xa9day", "genre": "\xa9gen", "comment": "\xa9cmt",
                           "album_artist": "aART", "track": "trkn"}
                audio = MP4(str(fp))
                for k, v in meta.items():
                    if k in SKIP or not v: continue
                    if k == "track":
                        try:
                            n = int(str(v).split("/")[0])
                            audio["trkn"] = [(n, 0)]
                        except (ValueError, TypeError):
                            pass
                        continue
                    tag = M4A_MAP.get(k)
                    if tag: audio[tag] = [str(v)]
                audio.save()
            elif ext in (".flac", ".opus"):
                FLAC_MAP = {"title": "title", "artist": "artist", "album": "album",
                            "year": "date", "genre": "genre", "comment": "comment",
                            "album_artist": "albumartist", "track": "tracknumber"}
                audio = _open_vorbis(fp, ext)
                for k, v in meta.items():
                    if k in SKIP or not v: continue
                    tag = FLAC_MAP.get(k)
                    if tag: audio[tag] = [str(v)]
                audio.save()
            else:
                from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC, TCON, TPE2, TRCK
                ID3_MAP = {"title": ("TIT2", TIT2), "artist": ("TPE1", TPE1),
                           "album": ("TALB", TALB), "year": ("TDRC", TDRC),
                           "genre": ("TCON", TCON), "album_artist": ("TPE2", TPE2),
                           "track": ("TRCK", TRCK)}
                try:
                    tags = ID3(str(fp))
                except Exception:
                    from mutagen.mp3 import MP3
                    a2 = MP3(str(fp), ID3=ID3); a2.add_tags(); tags = a2.tags
                for k, v in meta.items():
                    if k in SKIP or not v: continue
                    if k in ID3_MAP:
                        key, cls = ID3_MAP[k]
                        tags[key] = cls(encoding=3, text=str(v))
                tags.save(str(fp))
            print(f"  {G}[+]{R} Metadata (without lyrics) imported to {fp.name}!")
    else:
        print(f"  {Y}[!]{R} Invalid choice.")


def meta_menu():
    _sub_box("Metadata Export / Import", [
        (1, "Extract", "save metadata + cover to file"),
        (2, "Load",    "import metadata / cover to song"),
        (0, "Back",    None),
    ], width=50, icon="⇄")
    try:
        sub = int(_ask(f"\n{V}  ❯{R} Pick: "))
    except ValueError:
        return
    if sub == 0:
        return
    elif sub == 1:
        fps = pick_files()
        for fp in fps:
            meta_extract(fp)
    elif sub == 2:
        meta_load()
    else:
        print(f"  {Y}[!]{R} Invalid choice.")

def _is_url_str(s):
    s = s.strip().lower()
    return s.startswith("http://") or s.startswith("https://")

def _split_urls(raw):
    """If every comma-separated piece of raw looks like a URL, return the
    list of URLs (whitespace-trimmed). Otherwise return None, meaning the
    input should be treated as a search query instead. Lets Search &
    Download take one or more pasted links directly -- ',' separates
    multiples -- instead of needing a dedicated 'Single URL' menu item."""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if parts and all(_is_url_str(p) for p in parts):
        return parts
    return None

def _is_url_file(txt_path):
    try:
        with open(txt_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = [l.strip() for l in f.readlines() if l.strip()]
        if not lines:
            return False
        url_lines = sum(1 for l in lines[:10] if "http" in l or "youtu" in l)
        return url_lines >= len(lines[:10]) * 0.5
    except Exception:
        return False


def _run_with_spinner(cmd, label="Processing"):
    """Run a command in background, show spinner. Return subprocess.CompletedProcess.
    Prints a start/done timestamp around the run so it's clear when a process
    (download, thumbnail fetch, etc.) actually began and finished."""
    frames  = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    stop    = threading.Event()
    box     = {}

    print(f"  {_ts()} {DIM}{label} — starting...{R}")

    def _worker():
        box["r"] = subprocess.run(cmd, capture_output=True, text=True)
        stop.set()

    def _spin():
        i = 0
        while not stop.is_set():
            print(f"\r  {C}{frames[i % len(frames)]}{R}  {DIM}{label}...{R}",
                  end="", flush=True)
            time.sleep(0.1)
            i += 1
        print(f"\r{' ' * (len(label) + 12)}\r", end="", flush=True)

    t1 = threading.Thread(target=_worker, daemon=True)
    t2 = threading.Thread(target=_spin,   daemon=True)
    t1.start(); t2.start()
    t1.join(); stop.set(); t2.join()
    print(f"  {_ts()} {DIM}{label} — done.{R}")
    return box["r"]

def _preview_thumbnail_from_url(thumb_url, label):
    """Download thumbnail from URL, crop, show via am start, return user choice Y/N/X."""
    import urllib.request, re
    try:
        hd_url = re.sub(r"w\d+-h\d+", "w720-h720", thumb_url)
        req = urllib.request.Request(hd_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            with open(THUMB_CACHE_PATH, "wb") as f:
                f.write(resp.read())
        _crop_square(THUMB_CACHE_PATH)
        while True:
            choice = input(f"  Show thumbnail preview? (Y/n/x): ").strip().lower()
            if choice in ('y', 'n', 'x'):
                break
        if choice == 'x':
            try:
                Path(THUMB_CACHE_PATH).unlink()
            except:
                pass
            return 'X'
        elif choice == 'n':
            try:
                Path(THUMB_CACHE_PATH).unlink()
            except:
                pass
            return 'N'
        else:
            _open_image_preview(THUMB_CACHE_PATH)
            print(f"  {C}[i]{R} Thumbnail shown for {label}.")
            time.sleep(1.5)
            try:
                Path(THUMB_CACHE_PATH).unlink()
            except:
                pass
            return 'Y'
    except Exception as e:
        print(f"  {Y}[!]{R} Failed to preview thumbnail: {e}")
        return None

def _preview_thumbnail_for_url(url, idx, total, skip_all_flag):
    """Show thumbnail for a URL, return 'Y' (shown), 'N' (skip this), 'X' (skip all), None on error."""
    try:
        cmd = [
            "yt-dlp", "--write-thumbnail", "--convert-thumbnails", "jpg",
            "--skip-download",
            "-o", "thumbnail:%(title)s",
            "-o", "%(title)s.%(ext)s",
            url
        ]
        before = set(Path(".").glob("*.jpg"))
        result = _run_with_spinner(cmd, f"Thumbnail preview {idx}/{total}")
        after = set(Path(".").glob("*.jpg"))
        new_files = after - before
        if not new_files:
            print(f"  {Y}[!]{R} Failed to get thumbnail for URL {idx}.")
            return None
        jpg = sorted(new_files, key=lambda x: x.stat().st_mtime, reverse=True)[0]
        import shutil
        shutil.copy(jpg, THUMB_CACHE_PATH)
        try:
            jpg.unlink()
        except:
            pass
        _crop_square(THUMB_CACHE_PATH)
        while True:
            choice = input(f"  Show thumbnail preview? (Y/n/x): ").strip().lower()
            if choice in ('y', 'n', 'x'):
                break
        if choice == 'x':
            try:
                Path(THUMB_CACHE_PATH).unlink()
            except:
                pass
            return 'X'
        elif choice == 'n':
            try:
                Path(THUMB_CACHE_PATH).unlink()
            except:
                pass
            return 'N'
        else:
            _open_image_preview(THUMB_CACHE_PATH)
            print(f"  {C}[i]{R} Thumbnail shown for URL {idx}.")
            time.sleep(1.5)
            try:
                Path(THUMB_CACHE_PATH).unlink()
            except:
                pass
            return 'Y'
    except Exception as e:
        print(f"  {Y}[!]{R} Error previewing thumbnail: {e}")
        return None

def _batch_download_from_urls(urls, fmt, quality, preview_limit=None, artist_map=None):
    if preview_limit is None:
        preview_limit = CONFIG.get("preview_threshold", 7)
    ok = fail = 0
    skip_all_previews = False
    for i, url in enumerate(urls, 1):
        if not skip_all_previews and i <= preview_limit:
            choice = _preview_thumbnail_for_url(url, i, len(urls), skip_all_previews)
            if choice == 'X':
                skip_all_previews = True
        override = (artist_map or {}).get(url)
        if _dl_run(url, fmt, quality, idx=i, total=len(urls), artists_override=override):
            ok += 1
        else:
            fail += 1
    _refresh_media_scan()
    return ok, fail

def _yt_download_thumbnail(url):
    """Download thumbnail with video title as filename. Returns (Path, None) or (None, reason)."""
    cmd = [
        "yt-dlp", "--write-thumbnail", "--convert-thumbnails", "jpg",
        "--skip-download",
        "-o", "thumbnail:%(title)s",
        "-o", "%(title)s.%(ext)s",
        url
    ]
    before = set(Path(".").glob("*.jpg"))
    result = _run_with_spinner(cmd, "Downloading thumbnail")
    after  = set(Path(".").glob("*.jpg"))
    new_files = after - before
    if new_files:
        return sorted(new_files, key=lambda x: x.stat().st_mtime, reverse=True)[0], None
    err_text = (result.stderr or result.stdout or "").strip()
    reason   = next((l.strip() for l in err_text.splitlines() if "ERROR" in l), err_text[:120] or "?")
    return None, reason

def _batch_embed_thumbnails(downloaded):
    audio_files = scan_audio()
    if not audio_files:
        print(f"{Y}[!]{R} {_scan_empty_message('No audio files in this folder.')}")
        return
    print(f"\n{LV}[i]{R} Matching thumbnails to songs...\n")
    any_embedded = False
    for jpg, src_title in downloaded:
        if not jpg or not jpg.exists():
            continue
        jpg_tokens = _tokenize(jpg.stem) | _tokenize(src_title)
        best_audio, best_score = None, 0.0
        for afp in audio_files:
            title, _ = _get_tags(afp)
            audio_tokens = _tokenize(title if title else afp.stem)
            if not jpg_tokens or not audio_tokens:
                continue
            s1 = _fuzzy_token_score(jpg_tokens, audio_tokens) / len(jpg_tokens)
            s2 = _fuzzy_token_score(audio_tokens, jpg_tokens) / len(audio_tokens)
            score = (2 * s1 * s2 / (s1 + s2)) if (s1 + s2) > 0 else 0
            if score > best_score:
                best_score, best_audio = score, afp
        if best_audio and best_score >= 0.3:
            print(f"  {LV}[i]{R} {jpg.name} → {best_audio.name}  (score: {best_score:.2f})")
            konfirm = input("  Embed? (Y/n): ").strip().lower()
            if konfirm != "n":
                try:
                    _crop_square(jpg)
                    _embed_cover(best_audio, jpg)
                    any_embedded = True
                    print(f"  {G}[+]{R} Done!")
                    try:
                        jpg.unlink()
                        print(f"  {DIM}[—] {jpg.name} deleted.{R}\n")
                    except Exception as e:
                        print(f"  {Y}[!]{R} Failed to delete {jpg.name}: {e}\n")
                except Exception as e:
                    print(f"  {Y}[!]{R} Failed: {e}\n")
            else:
                print(f"  {C}[~]{R} Skipped.\n")
        else:
            print(f"  {Y}[!]{R} No match for {jpg.name}\n")

    # Android/music players commonly cache the old cover art per file — force
    # a media rescan (same trick used after song downloads) so the new
    # artwork actually shows up instead of silently looking "unchanged".
    if any_embedded:
        _refresh_media_scan()

def download_thumbnail():
    print("\nThumbnail download mode:")
    print("  1. Single URL")
    print("  2. From .txt (URL list) in this folder")
    print("  0. Back")
    try:
        sub = int(_ask("\nPick: "))
    except ValueError:
        sub = 0

    if sub == 0:
        return

    elif sub == 1:
        url = _ask("YouTube URL: ").strip()
        if not url:
            print("Cancelled.")
            return
        jpg, err = _yt_download_thumbnail(url)
        if not jpg:
            print(f"  {Y}[!]{R} Failed to download thumbnail.")
            if err:
                print(f"      Reason: {err}")
            return
        title = jpg.stem
        print(f"  {G}[+]{R} Saved: {jpg.name}")
        _batch_embed_thumbnails([(jpg, title)])

    elif sub == 2:
        txt_files = [f for f in sorted(Path(".").glob("*.txt")) if _is_url_file(f)]
        if not txt_files:
            print(f"\n{Y}[!]{R} No .txt file with URLs in this folder.")
            return
        if len(txt_files) > 1:
            print(f"\n{LV}[i]{R} URL files found:")
            for i, f in enumerate(txt_files, 1):
                print(f"  {i}. {f.name}")
            try:
                pick = int(_ask("\nPick file (number): ")) - 1
                if pick < 0 or pick >= len(txt_files):
                    print("Cancelled.")
                    return
                txt = txt_files[pick]
            except ValueError:
                print("Cancelled.")
                return
        else:
            txt = txt_files[0]
            print(f"\n{LV}[i]{R} Using: {txt.name}")

        with open(txt, "r", encoding="utf-8") as f:
            urls = [l.strip() for l in f if l.strip() and "http" in l]
        if not urls:
            print(f"{Y}[!]{R} No valid URLs in file.")
            return

        print(f"\n{_ts()} {LV}[i]{R} {len(urls)} URL(s). Starting download...\n")
        downloaded = []
        for i, url in enumerate(urls, 1):
            print(f"[{i}/{len(urls)}] {url[:60]}")
            jpg, err = _yt_download_thumbnail(url)
            if jpg:
                title = jpg.stem
                print(f"  {G}[+]{R} {jpg.name}")
                downloaded.append((jpg, title))
            else:
                print(f"  {Y}[!]{R} Failed.")
                if err:
                    print(f"      Reason: {err}")
        if downloaded:
            _batch_embed_thumbnails(downloaded)
        else:
            print(f"\n{Y}[!]{R} All downloads failed.")
    else:
        print("Invalid choice.")

def fetch_thumbnails_from_ytmusic():
    """Batch: scan all audio (with blacklist), search YTMusic, interactive per file."""
    try:
        from ytmusicapi import YTMusic
    except ImportError:
        print(f"  {Y}[!]{R} ytmusicapi not installed. Run: pip install ytmusicapi")
        return

    audio_files = scan_audio()
    if not audio_files:
        print(f"\n{Y}[!]{R} {_scan_empty_message('No audio files in this folder.')}")
        return

    items = _sorted_song_picks(audio_files)
    audio_files = [it[0] for it in items]
    rows = _song_pick_rows(items)
    _box("Song List", rows, min_width=50, icon="☰")

    blacklist_raw = _ask(f"\n  {C}Enter numbers of songs to skip (blacklist){R}, e.g., 1,4,5: ").strip()
    blacklist = _parse_multi_input(blacklist_raw, len(audio_files))
    blacklist_set = set(blacklist)

    tasks = []
    for idx, afp in enumerate(audio_files, 1):
        if idx in blacklist_set:
            print(f"  {DIM}[—] Skip {afp.name} (blacklist){R}")
            continue
        title, artist = _get_tags(afp)
        if not title:
            title = afp.stem  # fallback to filename when title tag is missing
        query = f"{artist} - {title}" if artist else title
        tasks.append((afp, query))

    if not tasks:
        print(f"\n{Y}[!]{R} No songs to process (all blacklisted).")
        return

    print(f"\n{_ts()} {C}Processing {len(tasks)} song(s)...{R}\n")
    yt = YTMusic()
    results_summary = {'good': 0, 'fail': 0}
    failed_files = []
    skip_all_previews = False

    for i, (afp, query) in enumerate(tasks, 1):
        _hdr = f"[{i}/{len(tasks)}] {afp.name}"
        _hw = max(30, len(_hdr) + 4)
        print(f"\n{V}  ╔═ {_hdr} {'═' * max(0, _hw - len(_hdr) - 3)}╗{R}")
        print(f"{V}  ╚{'═' * _hw}╝{R}")
        print(f"  {DIM}Query: {query}{R}\n")

        try:
            search_res = yt.search(query, filter="songs", limit=15)
        except Exception as e:
            print(f"    {Y}[!]{R} Search error: {e}")
            results_summary['fail'] += 1
            failed_files.append(afp.name)
            continue

        if not search_res:
            print(f"    {Y}[!]{R} No results.")
            results_summary['fail'] += 1
            failed_files.append(afp.name)
            continue

        for idx_res, res in enumerate(search_res, 1):
            res_title = res.get("title", "?")
            res_artist = ", ".join(a["name"] for a in res.get("artists", [])) or "?"
            vid = res.get("videoId", "")
            url = f"https://music.youtube.com/watch?v={vid}" if vid else ""
            print(f"  {P}{idx_res:2}{R}. {LV}{res_title[:45]}{R}")
            print(f"      {DIM}{res_artist[:40]}  {url}{R}")

        print(f"  {P} 0{R}. {DIM}Skip this song{R}")
        try:
            choice = int(_ask(f"\n  {C}Pick number (0 to skip){R}: "))
        except ValueError:
            choice = 0

        if choice == 0 or not (1 <= choice <= len(search_res)):
            print(f"  {DIM}Skipped.{R}")
            results_summary['fail'] += 1
            failed_files.append(afp.name)
            continue

        selected = search_res[choice - 1]
        thumb_url = selected.get("thumbnails", [])[-1]["url"] if selected.get("thumbnails") else None
        if not thumb_url:
            print(f"  {Y}[!]{R} No thumbnail for this result.")
            results_summary['fail'] += 1
            failed_files.append(afp.name)
            continue

        if not skip_all_previews:
            preview_choice = _preview_thumbnail_from_url(thumb_url, f"result #{choice} for {afp.name}")
            if preview_choice == 'X':
                skip_all_previews = True
            elif preview_choice == 'N':
                pass

        try:
            import urllib.request, re
            hd_url = re.sub(r"w\d+-h\d+", "w720-h720", thumb_url)
            req = urllib.request.Request(hd_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                temp_img = Path(f"temp_thumb_{afp.stem}.jpg")
                with open(temp_img, "wb") as f:
                    f.write(resp.read())
        except Exception as e:
            print(f"  {Y}[!]{R} Failed to download thumbnail: {e}")
            results_summary['fail'] += 1
            failed_files.append(afp.name)
            continue

        try:
            _crop_square(temp_img)
            _embed_cover(afp, temp_img)
            temp_img.unlink()
            _stamp_comment(afp, "Downloaded & Embedded by TagMan")
            results_summary['good'] += 1
            print(f"  {G}[+]{R} Thumbnail embedded to {afp.name}!")
        except Exception as e:
            print(f"  {Y}[!]{R} Failed to embed: {e}")
            results_summary['fail'] += 1
            failed_files.append(afp.name)
            try:
                temp_img.unlink()
            except:
                pass

    rows = [
        (f"  Successful embeds : {results_summary['good']}", f"  {G}Successful embeds :{R} {results_summary['good']}"),
        (f"  Failed/skipped    : {results_summary['fail']}", f"  {Y}Failed/skipped    :{R} {results_summary['fail']}"),
    ]
    if failed_files:
        rows.append((f"  Failed/skipped files:", f"  {DIM}Failed/skipped files:{R}"))
        for name in failed_files:
            rows.append((f"    • {name}", f"    {DIM}• {name}{R}"))
    _box("Summary", rows, min_width=50, icon="☰")
    print(f"  {DIM}Finished {_ts()}{R}")

    # Force a media rescan so players don't keep showing cached old artwork.
    if results_summary['good'] > 0:
        _refresh_media_scan()

def fetch_single_thumbnail_from_ytmusic():
    """Single: pick one song, search 15 results, pick, preview, embed."""
    try:
        from ytmusicapi import YTMusic
    except ImportError:
        print(f"  {Y}[!]{R} ytmusicapi not installed. Run: pip install ytmusicapi")
        return

    audio_files = scan_audio()
    if not audio_files:
        print(f"\n{Y}[!]{R} {_scan_empty_message('No audio files in this folder.')}")
        return

    items = _sorted_song_picks(audio_files)
    audio_files = [it[0] for it in items]
    rows = _song_pick_rows(items)
    rows.append((f"   0. Back", f"   {P}0{R}. {DIM}Back{R}"))
    _box("Pick Song", rows, min_width=50, icon="☰")

    try:
        pick = int(_ask(f"\n{V}  ❯{R} Number: "))
    except ValueError:
        return
    if pick == 0 or not (1 <= pick <= len(audio_files)):
        print(f"  {DIM}Cancelled.{R}")
        return

    afp = audio_files[pick - 1]
    title, artist = _get_tags(afp)
    if not title:
        title = afp.stem  # fallback to filename when title tag is missing
    query = f"{artist} - {title}" if artist else title

    print(f"\n  {C}Searching thumbnail for: {query}{R}")

    try:
        yt = YTMusic()
        results = yt.search(query, filter="songs", limit=15)
    except Exception as e:
        print(f"  {Y}[!]{R} Search failed: {e}")
        return

    if not results:
        print(f"  {Y}[!]{R} No results.")
        return

    rows = []
    for i, res in enumerate(results, 1):
        res_title = res.get("title", "?")
        res_artist = ", ".join(a["name"] for a in res.get("artists", [])) or "?"
        vid = res.get("videoId", "")
        url = f"https://music.youtube.com/watch?v={vid}" if vid else ""
        rows.append((f"  {i:2}. {res_title[:45]}", f"  {P}{i:2}{R}. {LV}{res_title[:45]}{R}"))
        rows.append((f"      {res_artist[:40]}  {url}", f"      {DIM}{res_artist[:40]}  {url}{R}"))
    rows.append((f"   0. Back", f"   {P}0{R}. {DIM}Back{R}"))
    _box("Search Results", rows, min_width=55, max_width=76, icon="✧")

    try:
        choice = int(_ask(f"\n{V}  ❯{R} Pick number: "))
    except ValueError:
        choice = 0

    if choice == 0 or not (1 <= choice <= len(results)):
        print(f"  {DIM}Cancelled.{R}")
        return

    selected = results[choice - 1]
    thumb_url = selected.get("thumbnails", [])[-1]["url"] if selected.get("thumbnails") else None
    if not thumb_url:
        print(f"  {Y}[!]{R} No thumbnail for this result.")
        return

    preview_choice = _preview_thumbnail_from_url(thumb_url, f"result #{choice} for {afp.name}")
    if preview_choice == 'N' or preview_choice == 'X':
        konfirm = input(f"  Continue embedding without preview? (Y/n): ").strip().lower()
        if konfirm == "n":
            print(f"  {DIM}Cancelled.{R}")
            return

    try:
        import urllib.request, re
        hd_url = re.sub(r"w\d+-h\d+", "w720-h720", thumb_url)
        req = urllib.request.Request(hd_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            temp_img = Path(f"temp_thumb_{afp.stem}.jpg")
            with open(temp_img, "wb") as f:
                f.write(resp.read())
    except Exception as e:
        print(f"  {Y}[!]{R} Failed to download thumbnail: {e}")
        return

    try:
        _crop_square(temp_img)
        _embed_cover(afp, temp_img)
        temp_img.unlink()
        _stamp_comment(afp, "Downloaded & Embedded by TagMan")
        print(f"  {G}[+]{R} Thumbnail embedded to {afp.name}!")
        # Force a media rescan so players don't keep showing cached old artwork.
        _refresh_media_scan()
    except Exception as e:
        print(f"  {Y}[!]{R} Failed to embed: {e}")
        try:
            temp_img.unlink()
        except:
            pass

# ─── Search functions with fallback to yt-dlp ──────────────────────────────

def _search_songs(query, limit=30, include_videos=True):
    """Search YTMusic's 'songs' filter, and -- when include_videos is True --
    also its 'videos' filter (music videos, official-audio-as-video uploads,
    vocal covers etc. only show up under 'videos' and are otherwise
    completely invisible to Sorcerer even though they're right there under
    music.youtube.com), then merge in a yt-dlp ytsearch pass too instead of
    only using it as a last-resort fallback when YTMusic finds nothing.

    When include_videos is False (Settings: video_search_mode = 'never'),
    this behaves like the old songs-only search: yt-dlp only kicks in if
    YTMusic came back completely empty.

    Results are de-duplicated by videoId, keeping the first (YTMusic) hit
    since it carries proper artist/thumbnail metadata; yt-dlp entries only
    fill in the gaps."""
    seen = set()
    results = []

    def _add(entry):
        vid = entry.get("videoId") or ""
        key = vid or entry.get("title", "")
        if not key or key in seen:
            return
        seen.add(key)
        results.append(entry)

    try:
        from ytmusicapi import YTMusic
        yt = YTMusic()
        filters = ("songs", "videos") if include_videos else ("songs",)
        for filt in filters:
            try:
                yt_results = yt.search(query, filter=filt, limit=limit) or []
            except Exception:
                yt_results = []
            for r in yt_results:
                _add({
                    'title': r.get('title', '?'),
                    'artists': [{'name': a['name']} for a in r.get('artists', [])],
                    'duration': r.get('duration', '?'),
                    'videoId': r.get('videoId', ''),
                    'thumbnails': r.get('thumbnails', []),
                    # Came straight from YTMusic's structured artist data --
                    # this is authoritative and safe to write to tags as-is.
                    'artist_source': 'ytmusic',
                })
    except Exception:
        pass

    # 'always'/'ask-yes': merge in yt-dlp whenever there's still room left.
    # 'never': only fall back to yt-dlp if YTMusic found nothing at all,
    # same as the original songs-only behavior.
    run_ytdlp = (len(results) < limit) if include_videos else (not results)

    if run_ytdlp:
        try:
            cmd = [
                "yt-dlp", f"ytsearch{limit}:{query}",
                "--flat-playlist", "--dump-json", "--no-warnings"
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            if proc.returncode == 0:
                for line in proc.stdout.strip().splitlines():
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        thumb = data.get('thumbnail', '')
                        _add({
                            'title': data.get('title', '?'),
                            'artists': [{'name': data.get('uploader', '?')}],
                            'duration': str(data.get('duration', '?')),
                            'videoId': data.get('id', ''),
                            'thumbnails': [{'url': thumb}] if thumb else [],
                            # This 'artist' is really just the uploading
                            # channel name (yt-dlp's flat-playlist search has
                            # no structured artist data) -- same guesswork
                            # quality as the embedded-metadata fallback, not
                            # authoritative, so don't use it to override tags.
                            'artist_source': 'ytdlp',
                        })
                    except json.JSONDecodeError:
                        continue
        except Exception:
            pass

    if not results:
        print(f"  {DIM}No matches from YTMusic or YouTube search.{R}")

    return results[:limit]

def _resolve_video_search_pref():
    """Resolve whether this search session should include the extra
    (slower) YTMusic 'videos' filter + yt-dlp merge pass, per the
    video_search_mode Settings preference: 'always' -> yes, 'never' -> no,
    'ask' -> prompt once per session."""
    mode = CONFIG.get("video_search_mode", "ask")
    if mode == "always":
        return True
    if mode == "never":
        return False
    ans = input(f"  Also search videos (slower, catches covers/video-only uploads)? (Y/n): ").strip().lower()
    return ans != "n"

# ─── Downloader functions ──────────────────────────────────────────────────

def _ytm_search_and_download(fmt, quality):
    """Search YouTube Music (fallback to YouTube), preview thumbnail, download.
    Also accepts one or more links pasted directly (comma-separated for
    multiples), skipping search entirely -- this is what replaced the old
    standalone 'Single URL' menu item."""
    import re, urllib.request
    query = _ask(f"\n  {C}Magic works with belief{R}: ").strip()
    if not query:
        return

    urls = _split_urls(query)
    if urls:
        if len(urls) == 1:
            url = urls[0]
            _get_session_destination()
            _preview_thumbnail_for_url(url, 1, 1, False)
            _print_patience_banner()
            _dl_run(url, fmt, quality)
            _refresh_media_scan()
        else:
            print(f"\n{_ts()} {LV}[i]{R} {len(urls)} URL(s) | {_fmt_quality_label(fmt, quality)}\n")
            _get_session_destination()
            _print_patience_banner()
            ok, fail = _batch_download_from_urls(urls, fmt, quality)
            print(f"  {G}[+]{R} Done! {ok} succeeded, {fail} failed. {_ts()}")
        return

    include_videos = _resolve_video_search_pref()
    print(f"  {DIM}Searching...{R}")
    results = _search_songs(query, limit=37, include_videos=include_videos)
    if not results:
        print(f"  {Y}[!]{R} No results for: {query}")
        return

    while True:
        q_short = query[:35]
        rows = []
        for i, r in enumerate(results, 1):
            title    = r.get("title", "?")
            artists  = ", ".join(a["name"] for a in r.get("artists", [])) or "?"
            duration = r.get("duration") or "?"
            vid      = r.get("videoId", "")
            url_str  = f"https://music.youtube.com/watch?v={vid}" if vid else ""
            rows.append((f"  {i:2}. {title[:45]}", f"  {P}{i:2}{R}. {LV}{title[:45]}{R}"))
            rows.append((f"      {artists[:40]}  [{duration}]", f"      {DIM}{artists[:40]}  [{duration}]{R}"))
            if url_str:
                rows.append((f"      {url_str}", f"      {DIM}{url_str}{R}"))
        rows.append((f"   0. Back", f"   {P}0{R}. {DIM}Back{R}"))
        _box(f"Results: {q_short}", rows, min_width=55, max_width=76, icon="✧")

        try:
            pick = int(_ask(f"\n{V}  ❯{R} Pick: "))
        except ValueError:
            continue
        if pick == 0:
            return
        if not (1 <= pick <= len(results)):
            print(f"  {Y}[!]{R} Invalid choice.")
            continue

        chosen   = results[pick - 1]
        vid_id   = chosen.get("videoId", "")
        title    = chosen.get("title", "?")
        artists  = ", ".join(a["name"] for a in chosen.get("artists", [])) or "?"
        url      = f"https://music.youtube.com/watch?v={vid_id}"

        if not vid_id:
            print(f"  {Y}[!]{R} Video ID not available, pick again.")
            continue

        print(f"\n  {LV}{title}{R}  —  {DIM}{artists}{R}")
        print(f"  {DIM}{url}{R}")

        _get_session_destination()

        thumbs = chosen.get("thumbnails", [])
        thumb_ok = False
        if thumbs:
            raw_url = thumbs[-1]["url"]
            hd_url  = re.sub(r"w\d+-h\d+", "w720-h720", raw_url)
            try:
                req = urllib.request.Request(hd_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    with open(THUMB_CACHE_PATH, "wb") as tf:
                        tf.write(resp.read())
                _open_image_preview(THUMB_CACHE_PATH)
                thumb_ok = True
            except Exception as e:
                print(f"  {Y}[!]{R} Thumbnail failed: {e}")

        konfirm = input(f"\n  Download this song? (Y/n): ").strip().lower()
        if konfirm == "n":
            if thumb_ok:
                try: Path(THUMB_CACHE_PATH).unlink()
                except: pass
            continue

        print(f"  {LV}[i]{R} {_fmt_quality_label(fmt, quality)}\n")
        artists_override = ([a["name"] for a in chosen.get("artists", [])]
                             if chosen.get("artist_source") == "ytmusic" else None)
        _print_patience_banner()
        _dl_run(url, fmt, quality, artists_override=artists_override)

        if thumb_ok:
            try:
                Path(THUMB_CACHE_PATH).unlink()
                print(f"  {DIM}[—] tagman_thumb.jpg deleted.{R}")
            except: pass
        break

    _refresh_media_scan()

def _ytm_batch_search_queue(fmt, quality):
    try:
        from ytmusicapi import YTMusic
    except ImportError:
        print(f"  {Y}[!]{R} ytmusicapi not installed. Run: pip install ytmusicapi")
        return

    try:
        yt = YTMusic()
    except Exception as e:
        print(f"  {Y}[!]{R} YTMusic init failed: {e}")
        return

    queue = []
    preview_limit = CONFIG.get("preview_threshold", 7)
    include_videos = _resolve_video_search_pref()

    while True:
        query = _ask(f"\n  {C}Magic works with belief{R}: ").strip()
        if not query:
            if queue:
                break
            return

        urls = _split_urls(query)
        if urls:
            for u in urls:
                queue.append((u, "?", u, None))
            print(f"\n  {G}[+]{R} Added {len(urls)} link(s) to queue.")
            print(f"  {DIM}Total in queue: {len(queue)} song(s){R}")
            while True:
                again = input(f"\n  Search another song? (s = yes, continue searching / d = done): ").strip().lower()
                if again in ("s", "d"):
                    break
                print(f"  {Y}[!]{R} Type 's' or 'd'.")
            if again == "d":
                break
            continue

        print(f"  {DIM}Searching...{R}")
        results = _search_songs(query, limit=37, include_videos=include_videos)
        if not results:
            print(f"  {Y}[!]{R} No results for: {query}")
        else:
            q_short = query[:35]
            rows = []
            for i, r in enumerate(results, 1):
                title    = r.get("title", "?")
                artists  = ", ".join(a["name"] for a in r.get("artists", [])) or "?"
                duration = r.get("duration") or "?"
                vid      = r.get("videoId", "")
                url_str  = f"https://music.youtube.com/watch?v={vid}" if vid else ""
                rows.append((f"  {i:2}. {title[:45]}", f"  {P}{i:2}{R}. {LV}{title[:45]}{R}"))
                rows.append((f"      {artists[:40]}  [{duration}]", f"      {DIM}{artists[:40]}  [{duration}]{R}"))
                if url_str:
                    rows.append((f"      {url_str}", f"      {DIM}{url_str}{R}"))
            rows.append((f"   0. Skip this search", f"   {P}0{R}. {DIM}Skip this search{R}"))
            _box(f"Results: {q_short}", rows, min_width=55, max_width=76, icon="✧")

            raw = _ask(f"\n{V}  ❯{R} Song number(s) (multi, e.g., 1,3,5): ").strip()
            if raw and raw != "0":
                picks = _parse_multi_input(raw, len(results))
                added = []
                for p in picks:
                    r   = results[p - 1]
                    vid = r.get("videoId", "")
                    if not vid:
                        continue
                    title   = r.get("title", "?")
                    artist_names = [a["name"] for a in r.get("artists", [])]
                    artists = ", ".join(artist_names) or "?"
                    url     = f"https://music.youtube.com/watch?v={vid}"
                    override = artist_names if r.get("artist_source") == "ytmusic" else None
                    queue.append((title, artists, url, override))
                    added.append(title)
                if added:
                    shown = ", ".join(added[:5])
                    extra = f" +{len(added)-5} more" if len(added) > 5 else ""
                    print(f"\n  {G}[+]{R} Added: {shown}{extra}")
                    print(f"  {DIM}Total in queue: {len(queue)} song(s){R}")
                else:
                    print(f"  {Y}[!]{R} No valid numbers selected.")
            else:
                print(f"  {DIM}No songs added from this search.{R}")

        while True:
            again = input(f"\n  Search another song? (s = yes, continue searching / d = done): ").strip().lower()
            if again in ("s", "d"):
                break
            print(f"  {Y}[!]{R} Type 's' or 'd'.")
        if again == "d":
            break

    if not queue:
        print(f"\n  {DIM}No songs in queue. Cancelled.{R}")
        return

    preview_count = min(len(queue), preview_limit)
    if preview_count > 0:
        note_plain = (f"  {len(queue)} songs in queue. Previewing all of them."
                      if len(queue) <= preview_limit else
                      f"  {len(queue)} songs in queue. Previews available for the first {preview_limit}.")
        note_color = (f"  {C}{len(queue)}{R} songs in queue. Previewing all of them."
                      if len(queue) <= preview_limit else
                      f"  {C}{len(queue)}{R} songs in queue. Previews available for the first {preview_limit}.")
        rows = [
            (note_plain, note_color),
            (f"  Y = show image, N = skip this song, X = skip all",
             f"  {DIM}Y = show image, N = skip this song, X = skip all{R}"),
        ]
        _box("Thumbnail Preview", rows, min_width=50, icon="◆")
        skip_all_thumbs = False
        for _qi, (_qt, _qa, _qu, _qo) in enumerate(queue[:preview_limit], 1):
            if skip_all_thumbs:
                break
            print(f"\n  [{_qi}/{preview_limit}] {LV}{_qt}{R}  {DIM}— {_qa}{R}")
            _ans = input(f"  Show thumbnail preview? (Y/n/X): ").strip().lower()
            if _ans == "x":
                skip_all_thumbs = True
                print(f"  {DIM}Previews skipped for all.{R}")
                break
            elif _ans == "n":
                continue
            else:
                try:
                    import shutil
                    cmd = [
                        "yt-dlp", "--write-thumbnail", "--convert-thumbnails", "jpg",
                        "--skip-download",
                        "-o", "thumbnail:%(title)s",
                        "-o", "%(title)s.%(ext)s",
                        _qu
                    ]
                    before = set(Path(".").glob("*.jpg"))
                    _run_with_spinner(cmd, f"Thumbnail preview {_qi}/{preview_limit}")
                    after = set(Path(".").glob("*.jpg"))
                    new_files = after - before
                    if new_files:
                        jpg = sorted(new_files, key=lambda x: x.stat().st_mtime, reverse=True)[0]
                        shutil.copy(jpg, THUMB_CACHE_PATH)
                        try:
                            jpg.unlink()
                        except:
                            pass
                        _crop_square(THUMB_CACHE_PATH)
                        _open_image_preview(THUMB_CACHE_PATH)
                        print(f"  {G}[+]{R} Thumbnail opened.")
                        time.sleep(1.5)
                        try:
                            Path(THUMB_CACHE_PATH).unlink()
                        except:
                            pass
                    else:
                        print(f"  {Y}[!]{R} Failed to get thumbnail.")
                except Exception as _te:
                    print(f"  {Y}[!]{R} Failed to open thumbnail: {_te}")

    rows = []
    for i, (title, artists, url, _override) in enumerate(queue, 1):
        rows.append((f"  {i:2}. {title[:40]}  — {artists[:25]}", f"  {P}{i:2}{R}. {LV}{title[:40]}{R}  {DIM}— {artists[:25]}{R}"))
    _box(f"Download Queue ({len(queue)} songs)", rows, min_width=50, icon="✧")

    _get_session_destination()
    konfirm = input(f"\n  Download all {len(queue)} songs now? (Y/n): ").strip().lower()
    if konfirm == "n":
        print(f"  {DIM}Cancelled.{R}")
        return

    txt_name = _random_filename(prefix="bd", ext="txt")
    txt_path = Path(txt_name)
    with open(txt_path, "w", encoding="utf-8") as f:
        for _, _, url, _override in queue:
            f.write(url + "\n")

    print(f"\n  {LV}[i]{R} {_fmt_quality_label(fmt, quality)}")
    print(f"  {DIM}[~] Queue saved temporarily to {txt_name}{R}\n")

    urls = [u for _, _, u, _o in queue]
    artist_map = {u: o for _, _, u, o in queue if o}
    # Previews were already offered above, before this confirmation prompt —
    # don't ask again mid-download.
    _print_patience_banner()
    ok, fail = _batch_download_from_urls(
        urls, fmt, quality,
        preview_limit=0,
        artist_map=artist_map,
    )

    try:
        txt_path.unlink()
        print(f"  {DIM}[—] {txt_name} deleted.{R}")
    except Exception:
        pass

    print(f"  {G}[+]{R} Done! {ok} succeeded, {fail} failed. {_ts()}")

def _fmt_quality_label(fmt, quality):
    """Display string for the 'Format: ... | Quality: ...' line shown before
    a download. FLAC is lossless -- there's no bitrate choice behind it (see
    _fmt_quality_flags above), so showing a "Quality: best" next to it is
    just confusing/meaningless. Every other format still shows both."""
    if fmt == "flac":
        return f"Format: {fmt.upper()}"
    return f"Format: {fmt.upper()} | Quality: {quality}"

def _fmt_quality_flags(fmt, quality):
    """Shared -x/--audio-format/-S flags for a given format+quality combo.
    Used by both the single/batch downloader and the playlist downloader so
    the two never drift out of sync.

    FLAC is lossless -- there's no target bitrate to sort by, so it always
    just grabs the best available source audio and converts it as-is.
    "quality" is ignored for flac (kept in the signature only so callers
    don't need a special case).

    320/968 kbps are universal -- available for every non-flac format, not
    just opus. They force itag 251 (YouTube's own opus-audio stream) as the
    source and use --audio-quality to actually re-encode at that target
    bitrate, since a plain -S abr:X (used for 160/192/256 below) only steers
    stream *selection* and won't reliably get you there. Whether the result
    actually reaches that bitrate still depends on what data yt-dlp can pull
    for that particular video -- if it can't, tell the user to drop back to
    a lower Quality setting. (Reference: ytdlnis' equivalent yt-dlp
    invocation.)

    Opus's own "best" quality just keeps whatever native opus stream
    YouTube already serves (no forced re-encode) -- normal, no different
    from any other format's Default."""
    if fmt == "flac":
        return ["-f", "ba/b", "-x", "--audio-format", "flac"]
    if quality in ("320", "968"):
        return ["-f", "251/ba/b", "-x", "--audio-format", fmt, "-S", "aext:opus",
                 "--audio-quality", f"{quality}k"]
    if fmt == "opus":
        # "best" (native passthrough) is the only tier left for opus here --
        # 320/968 are handled by the shared branch above.
        return ["-f", "ba/b", "-x", "--audio-format", "opus", "-S", "aext:opus"]
    if fmt == "m4a":
        if quality == "best":
            return ["-f", "ba/b", "-x", "--audio-format", "m4a", "-S", "aext:m4a"]
        return ["-x", "--audio-format", "m4a", "-S", f"abr:{quality},aext:m4a"]
    # mp3
    if quality == "best":
        return ["-f", "ba/b", "-x", "--audio-format", "mp3"]
    return ["-x", "--audio-format", "mp3", "-S", f"abr:{quality}"]

def _build_yt_cmd(url, fmt, quality):
    cmd = ["yt-dlp", "--no-mtime", "-o", "%(uploader).30B - %(title).170B.%(ext)s"]
    cmd += _fmt_quality_flags(fmt, quality)
    cmd += _YT_META_FLAGS
    cmd.append(url)
    return cmd

def _build_yt_playlist_cmd(url, fmt, quality):
    """Same format/quality flags as _build_yt_cmd, but keeps the playlist
    index in the file name and tells yt-dlp to grab every item in the
    playlist instead of just the first video."""
    cmd = ["yt-dlp", "--no-mtime", "--yes-playlist",
           "-o", "%(playlist_index)s - %(uploader).30B - %(title).170B.%(ext)s"]
    cmd += _fmt_quality_flags(fmt, quality)
    cmd += _YT_META_FLAGS
    cmd.append(url)
    return cmd

def _apply_ytmusic_artists(fp, artist_names):
    """Overwrite ARTIST/ALBUM ARTIST with structured artist data straight
    from YTMusic's own API (see 'artist_source' == 'ytmusic' in
    _search_songs), instead of trusting yt-dlp's heuristic string-splitting
    of the video's embedded metadata. This is what actually fixes cases
    like a video's artist field being "Marianne Beaulieu / Walmoods" or a
    writer-credits dump joined with commas -- there's no delimiter to guess
    at here, YTMusic already told us exactly who the artists are.
    ARTIST gets every listed artist (joined with ", "); ALBUM ARTIST gets
    just the first/primary one, matching the convention used elsewhere in
    TagMan for multi-artist tracks."""
    names = [n for n in (artist_names or []) if n and n.strip()]
    if not names:
        return
    artist_val = ", ".join(names)
    album_artist_val = names[0]
    ext = Path(fp).suffix.lower()
    try:
        if ext == ".m4a":
            from mutagen.mp4 import MP4
            audio = MP4(str(fp))
            audio["\xa9ART"] = [artist_val]
            audio["aART"] = [album_artist_val]
            audio.save()
        elif ext in (".flac", ".opus"):
            audio = _open_vorbis(fp, ext)
            audio["artist"] = [artist_val]
            audio["albumartist"] = [album_artist_val]
            audio.save()
        else:
            from mutagen.id3 import ID3, TPE1, TPE2
            audio = ID3(str(fp))
            audio["TPE1"] = TPE1(encoding=3, text=artist_val)
            audio["TPE2"] = TPE2(encoding=3, text=album_artist_val)
            audio.save()
        print(f"  {DIM}[~] Artist tag corrected from YTMusic data → {artist_val}{R}")
    except Exception as e:
        print(f"  {Y}[!]{R} Couldn't apply YTMusic artist data: {e}")

def _post_process_download(dl_fp, thumb_url=None, artists_override=None, source_url=None, history_action="Downloaded"):
    """Shared post-download step: stamp the TagMan comment, patch blank
    copyright/composer/genre with the 'TN' fallback, and manually embed
    cover art if yt-dlp's own --embed-thumbnail step silently failed (known
    to happen on some MP3s). Used by both single-file and playlist
    downloads so the two never drift out of sync.

    artists_override: structured artist names straight from YTMusic's API
    (see _apply_ytmusic_artists), used to correct whatever yt-dlp's own
    metadata-parsing heuristic wrote for ARTIST/ALBUM ARTIST. Only pass this
    when the download came from a YTMusic search result -- for raw URLs
    there's no authoritative source to fall back on.

    source_url/history_action: recorded in History (Settings -> History) so
    downloads show up with their source link. history_action lets callers
    like Redownload log a more specific action than the plain "Downloaded"
    default."""
    if not dl_fp or not Path(dl_fp).exists():
        return
    _SESSION_DOWNLOADED_FILES.append(Path(dl_fp))
    _stamp_comment(dl_fp, "Downloaded & Embedded by TagMan")
    if artists_override:
        _apply_ytmusic_artists(dl_fp, artists_override)
    _ensure_fallback_fields(dl_fp, ("copyright", "composer", "genre"), "TN")
    _ensure_album_fallback(dl_fp)
    if not _has_cover_art(dl_fp) and thumb_url and thumb_url.lower() not in ("", "none", "na", "n/a"):
        tmp_img = Path(dl_fp).with_name(Path(dl_fp).stem + "_covertmp.jpg")
        try:
            import urllib.request
            req = urllib.request.Request(thumb_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                with open(tmp_img, "wb") as f:
                    f.write(resp.read())
            _crop_square(tmp_img)
            _embed_cover(dl_fp, tmp_img)
        except Exception:
            pass
        finally:
            # Always clean up the temp file, no matter what happened above --
            # previously this only ran on the pure-success path, so any
            # exception (even one fired AFTER the tag was already written)
            # left the "_covertmp.jpg" sitting next to the song forever.
            try:
                if tmp_img.exists():
                    tmp_img.unlink()
            except Exception:
                pass
        # Ground truth check: re-read the tag instead of trusting whether an
        # exception happened to propagate. This matters because _embed_cover()
        # can finish writing+saving successfully and still raise on some later
        # internal step -- the old code would then print "failed" even though
        # the cover was genuinely embedded.
        if _has_cover_art(dl_fp):
            print(f"  {DIM}[~] Cover art embedded manually for {Path(dl_fp).name} (yt-dlp's thumbnail step didn't take).{R}")
        else:
            print(f"  {Y}[!]{R} Couldn't embed cover art manually for {Path(dl_fp).name} either — check tags to confirm.")
    _history_log("download", f"{history_action}: {Path(dl_fp).name}", file=Path(dl_fp).name, url=source_url)

def _dl_run_playlist(url, fmt, quality):
    """Download every track in a playlist. Unlike _dl_run (one file, one
    result), yt-dlp prints one TAGMAN_FILEPATH::/TAGMAN_THUMBNAIL:: pair per
    track that finishes -- collect and post-process every one of them."""
    print(f"  {url[:65]}")
    cmd    = _build_yt_playlist_cmd(url, fmt, quality)
    result = _run_with_spinner(cmd, f"Downloading playlist ({fmt.upper()})")

    lines  = (result.stdout or "").splitlines()
    fps    = [l.split("TAGMAN_FILEPATH::", 1)[1].strip()  for l in lines if l.startswith("TAGMAN_FILEPATH::")]
    thumbs = [l.split("TAGMAN_THUMBNAIL::", 1)[1].strip() for l in lines if l.startswith("TAGMAN_THUMBNAIL::")]

    if not fps:
        err    = (result.stderr or result.stdout or "").strip().splitlines()
        reason = next((l for l in err if "ERROR" in l), err[-1] if err else "?")
        print(f"  {Y}[!]{R} Failed — no tracks were downloaded.")
        print(f"      Reason: {reason}\n")
        return 0, 1

    ok = 0
    for i, dl_fp in enumerate(fps):
        thumb_url = thumbs[i] if i < len(thumbs) else None
        if dl_fp and Path(dl_fp).exists():
            _post_process_download(dl_fp, thumb_url, source_url=url, history_action="Downloaded (playlist)")
            ok += 1

    if result.returncode != 0:
        print(f"  {Y}[!]{R} yt-dlp reported an error partway through — some tracks in the playlist may be missing.")
    print(f"  {G}[+]{R} {ok} track(s) downloaded & tagged.\n")
    return ok, (0 if ok else 1)

_RETRYABLE_ERRORS = ("403", "429", "500", "502", "503", "timed out", "connection reset")
_MAX_DL_RETRIES = 5

def _dl_run(url, fmt, quality, idx=None, total=None, artists_override=None, history_action="Downloaded"):
    prefix = f"[{idx}/{total}] " if idx else ""
    print(f"  {prefix}{url[:65]}")
    cmd = _build_yt_cmd(url, fmt, quality)

    attempt = 0
    while True:
        attempt += 1
        label = f"Downloading {fmt.upper()}" if attempt == 1 else f"Downloading {fmt.upper()} (retry {attempt - 1}/{_MAX_DL_RETRIES})"
        result = _run_with_spinner(cmd, label)

        if result.returncode == 0:
            fp_line = next((l for l in (result.stdout or "").splitlines()
                             if l.startswith("TAGMAN_FILEPATH::")), None)
            thumb_line = next((l for l in (result.stdout or "").splitlines()
                                if l.startswith("TAGMAN_THUMBNAIL::")), None)
            dl_fp     = fp_line.split("TAGMAN_FILEPATH::", 1)[1].strip() if fp_line else None
            thumb_url = thumb_line.split("TAGMAN_THUMBNAIL::", 1)[1].strip() if thumb_line else None
            _post_process_download(dl_fp, thumb_url, artists_override=artists_override, source_url=url, history_action=history_action)
            print(f"  {G}[+]{R} Done!\n")
            return True

        err    = (result.stderr or result.stdout or "").strip().splitlines()
        reason = next((l for l in err if "ERROR" in l), err[-1] if err else "?")
        hints = {
            "403": "403 Forbidden — possibly geo-block, age restriction, or premium video.",
            "404": "404 Not Found — invalid URL or video deleted.",
            "429": "429 Too Many Requests — rate limited, try again later.",
            "410": "410 Gone — video permanently deleted.",
            "Private video": "Video made private by owner.",
            "members-only": "Members-only video.",
            "Sign in": "Video requires login (age-restricted or private).",
            "copyright": "Video blocked due to copyright in your region.",
            "Requested format is not available": "This video doesn't have the source data for the selected Quality — try a lower Quality setting.",
            "not available": "Video not available in your region.",
        }
        hint = next((v for k, v in hints.items() if k.lower() in reason.lower()), None)
        is_retryable = any(k.lower() in reason.lower() for k in _RETRYABLE_ERRORS)

        if is_retryable and attempt <= _MAX_DL_RETRIES:
            print(f"  {Y}[!]{R} Attempt {attempt} failed{f' ({hint})' if hint else ''} — retrying ({attempt}/{_MAX_DL_RETRIES})...")
            time.sleep(min(2 * attempt, 10))
            continue

        print(f"  {Y}[!]{R} Failed{f' after {attempt} attempt(s)' if attempt > 1 else ''}.")
        if hint:
            print(f"      {hint}")
        else:
            print(f"      Reason: {reason}")
        print()
        return False

def _random_filename(prefix="bd", ext="txt"):
    import random, string
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=5))
    return f"{prefix}_{suffix}.{ext}"

# ─── Redownload (Sorcerer) ──────────────────────────────────────────────────
# Re-fetches songs already sitting in the folder: builds a search query from
# each file's own tags (falling back to the filename when a tag is blank,
# same fallback fetch_thumbnails_from_ytmusic() uses), auto-picks whichever
# search result looks most like the original via fuzzy token overlap, asks
# for a quick Y/n confirm, and only falls back to a manual top-10 pick list
# if that best guess gets rejected.

def _score_match(orig_title, orig_artist, result):
    """0..1 similarity between a song's own tags and a YTMusic/yt-dlp search
    result, using the same fuzzy token-overlap (Dice coefficient) approach
    _match_img_to_audio() uses for cover-art matching."""
    r_title = result.get("title", "") or ""
    r_artist = ", ".join(a["name"] for a in result.get("artists", [])) or ""
    orig_tokens = _tokenize(orig_title or "")
    if orig_artist:
        orig_tokens |= _tokenize(orig_artist)
    res_tokens = _tokenize(r_title) | _tokenize(r_artist)
    if not orig_tokens or not res_tokens:
        return 0.0
    s1 = _fuzzy_token_score(orig_tokens, res_tokens) / len(orig_tokens)
    s2 = _fuzzy_token_score(res_tokens, orig_tokens) / len(res_tokens)
    return (2 * s1 * s2 / (s1 + s2)) if (s1 + s2) > 0 else 0.0

def _show_search_result(r, n=None):
    r_title = r.get("title", "?")
    r_artist = ", ".join(a["name"] for a in r.get("artists", [])) or "?"
    vid = r.get("videoId", "")
    url = f"https://music.youtube.com/watch?v={vid}" if vid else ""
    if n is not None:
        print(f"  {P}{n:2}{R}. {LV}{r_title[:45]}{R}")
    else:
        print(f"      {LV}{r_title[:45]}{R}")
    print(f"      {DIM}{r_artist[:40]}  {url}{R}")

def _redownload_one(afp, orig_title, orig_artist, query, fmt, quality, include_videos, idx=None, total=None):
    """Redownload a single already-on-disk song: search using its own tags,
    auto-pick the closest match, confirm, and fall back to a manual top-10
    pick if the auto-pick is rejected. Returns True on a successful
    download."""
    hdr = f"[{idx}/{total}] {afp.name}" if idx else afp.name
    hw = max(30, len(hdr) + 4)
    print(f"\n{V}  ╔═ {hdr} {'═' * max(0, hw - len(hdr) - 3)}╗{R}")
    print(f"{V}  ╚{'═' * hw}╝{R}")
    print(f"  {DIM}Query: {query}{R}\n")

    results = _search_songs(query, limit=15, include_videos=include_videos)
    if not results:
        print(f"  {Y}[!]{R} No results.")
        return False

    scored = sorted(results, key=lambda r: _score_match(orig_title, orig_artist, r), reverse=True)
    best = scored[0]
    best_score = _score_match(orig_title, orig_artist, best)

    if not best.get("videoId"):
        print(f"  {Y}[!]{R} Best match has no video ID.")
        return False

    print(f"  {LV}Best match{R} {DIM}(similarity {best_score:.0%}){R}:")
    _show_search_result(best)

    konfirm = input(f"\n  Use this match? (Y/n): ").strip().lower()
    chosen = None
    if konfirm != "n":
        chosen = best
    else:
        top10 = scored[:10]
        rows = []
        for n, r in enumerate(top10, 1):
            r_title = r.get("title", "?")
            r_artist = ", ".join(a["name"] for a in r.get("artists", [])) or "?"
            vid2 = r.get("videoId", "")
            url2 = f"https://music.youtube.com/watch?v={vid2}" if vid2 else ""
            rows.append((f"  {n:2}. {r_title[:45]}", f"  {P}{n:2}{R}. {LV}{r_title[:45]}{R}"))
            rows.append((f"      {r_artist[:40]}  {url2}", f"      {DIM}{r_artist[:40]}  {url2}{R}"))
        rows.append((f"   0. Skip this song", f"   {P}0{R}. {DIM}Skip this song{R}"))
        _box("Alternate matches", rows, min_width=55, max_width=76, icon="✧")
        try:
            pick = int(_ask(f"\n{V}  ❯{R} Pick: "))
        except ValueError:
            pick = 0
        if pick == 0 or not (1 <= pick <= len(top10)):
            print(f"  {DIM}Skipped.{R}")
            return False
        chosen = top10[pick - 1]

    vid = chosen.get("videoId", "")
    if not vid:
        print(f"  {Y}[!]{R} No video ID for that pick.")
        return False
    url = f"https://music.youtube.com/watch?v={vid}"
    artists_override = ([a["name"] for a in chosen.get("artists", [])]
                         if chosen.get("artist_source") == "ytmusic" else None)
    ok = _dl_run(url, fmt, quality, idx=idx, total=total, artists_override=artists_override,
                  history_action="Redownloaded")
    if ok:
        _carry_over_lyrics_and_cleanup(afp)
    return ok

def _carry_over_lyrics_and_cleanup(old_fp):
    """After a successful Redownload, pull just the lyrics field from the
    old file and embed it into the freshly downloaded replacement (the most
    recent entry in _SESSION_DOWNLOADED_FILES -- Redownload runs strictly
    one song at a time, so this is always the file that was just
    downloaded), then delete the old file so the folder doesn't end up with
    both the stale and the new copy sitting side by side."""
    if not _SESSION_DOWNLOADED_FILES:
        return
    new_fp = _SESSION_DOWNLOADED_FILES[-1]
    try:
        if new_fp.resolve() == Path(old_fp).resolve():
            return  # safety net: never actually the same file, but just in case
    except Exception:
        pass

    try:
        if _has_lyrics(old_fp):
            lyrics = _get_lyrics(old_fp)
            if lyrics:
                _embed_lyrics(new_fp, lyrics)
                print(f"  {DIM}[~] Lyrics carried over from {Path(old_fp).name}.{R}")
    except Exception as e:
        print(f"  {Y}[!]{R} Couldn't carry over lyrics: {e}")

    try:
        Path(old_fp).unlink()
        print(f"  {DIM}[—] Old file deleted: {Path(old_fp).name}{R}")
    except Exception as e:
        print(f"  {Y}[!]{R} Couldn't delete old file {Path(old_fp).name}: {e}")

def _redownload_menu():
    """Sorcerer -> Redownload. Single (multi-pick) or Batch (everything)
    over whatever scan_audio() finds (folder picker included, same as
    every other Sorcerer/scan feature). Picks get stashed in a temp,
    randomly-named JSON file for the duration of the run -- purely a
    working file, deleted once the run finishes."""
    audio_files = scan_audio()
    if not audio_files:
        print(f"\n{Y}[!]{R} {_scan_empty_message('No audio files in this folder.')}")
        return

    _sub_box("Redownload", [
        (1, "Single", "pick song(s) to redownload"),
        (2, "Batch",  "redownload every song in this folder"),
        (0, "Back",   None),
    ], width=52, icon="⟳")
    try:
        rm = int(_ask(f"\n{V}  ❯{R} Pick: "))
    except ValueError:
        return
    if rm == 0:
        return
    if rm not in (1, 2):
        print(f"  {Y}[!]{R} Invalid choice.")
        return

    if rm == 1:
        items = _sorted_song_picks(audio_files)
        rows = _song_pick_rows(items)
        _box("Pick Song(s)", rows, min_width=50, icon="☰")
        raw = _ask(f"\n  {C}Number(s) to redownload{R}, e.g., 1,3,5: ").strip()
        picks = _parse_multi_input(raw, len(items))
        if not picks:
            print(f"  {DIM}Cancelled.{R}")
            return
        chosen_files = [items[p - 1][0] for p in picks]
    else:
        chosen_files = audio_files

    tasks = []
    for afp in chosen_files:
        title, artist = _get_tags(afp)
        orig_title = title or afp.stem  # fallback to filename when title tag is missing
        query = f"{artist} - {orig_title}" if artist else orig_title
        tasks.append((afp, orig_title, artist, query))

    # Stash the picks in a temp random JSON file, purely as working state
    # for this run -- cleaned up once the run finishes either way.
    json_path = Path(_random_filename(prefix="rdl", ext="json"))
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(
                [{"file": str(t[0]), "title": t[1], "artist": t[2], "query": t[3]} for t in tasks],
                f, indent=2, ensure_ascii=False
            )
    except Exception:
        json_path = None

    fmt, quality = _get_fmt_quality()
    if not fmt:
        if json_path:
            try: json_path.unlink()
            except Exception: pass
        return

    print(f"\n{_ts()} {LV}[i]{R} {len(tasks)} song(s) to redownload | {_fmt_quality_label(fmt, quality)}")
    if json_path:
        print(f"  {DIM}[~] Selection saved temporarily to {json_path.name}{R}")

    include_videos = _resolve_video_search_pref()
    _get_session_destination()
    _print_patience_banner()

    good = fail = 0
    failed_names = []
    for i, (afp, orig_title, artist, query) in enumerate(tasks, 1):
        ok = _redownload_one(afp, orig_title, artist, query, fmt, quality, include_videos, idx=i, total=len(tasks))
        if ok:
            good += 1
        else:
            fail += 1
            failed_names.append(afp.name)

    if json_path:
        try:
            json_path.unlink()
            print(f"  {DIM}[—] {json_path.name} deleted.{R}")
        except Exception:
            pass

    rows = [
        (f"  Redownloaded   : {good}", f"  {G}Redownloaded   :{R} {good}"),
        (f"  Skipped/failed : {fail}", f"  {Y}Skipped/failed :{R} {fail}"),
    ]
    if failed_names:
        rows.append((f"  Skipped/failed files:", f"  {DIM}Skipped/failed files:{R}"))
        for name in failed_names:
            rows.append((f"    • {name}", f"    {DIM}• {name}{R}"))
    _box("Summary", rows, min_width=50, icon="☰")
    print(f"  {DIM}Finished {_ts()}{R}")

def downloader():
    _SESSION_DOWNLOADED_FILES.clear()
    _maybe_reset_session_destination()
    _sub_box("Sorcerer", [
        (1, "Search & Download", "search songs, or paste link(s)"),
        (2, "Batch from .txt",   "download multiple URLs at once"),
        (3, "Download Playlist", "download an entire playlist from a URL"),
        (4, "Redownload",        "refetch songs already here, using their tags"),
        (0, "Back",              None),
    ], width=50, icon="✧")
    try:
        mode = int(_ask(f"\n{V}  ❯{R} Pick: "))
    except ValueError:
        return
    if mode == 0: return

    if mode == 1:
        _sub_box("Search & Download", [
            (1, "Single", "search, preview, download, or link(s)"),
            (2, "Batch",  "collect songs to download, or link(s)"),
            (0, "Back",   None),
        ], width=52, icon="✧")
        try:
            sm = int(_ask(f"\n{V}  ❯{R} Pick: "))
        except ValueError:
            return
        if sm == 0:
            return
        fmt, quality = _get_fmt_quality()
        if not fmt:
            return
        if sm == 1:
            _ytm_search_and_download(fmt, quality)
        elif sm == 2:
            _ytm_batch_search_queue(fmt, quality)
        else:
            print(f"  {Y}[!]{R} Invalid choice.")

    elif mode == 2:
        fmt, quality = _get_fmt_quality()
        if not fmt: return
        txt_files = [f for f in sorted(Path(".").glob("*.txt")) if _is_url_file(f)]
        if not txt_files:
            print(f"  {Y}[!]{R} No .txt file with URLs in this folder.")
            return
        if len(txt_files) > 1:
            print(f"  {LV}[i]{R} URL files found:")
            for i, f in enumerate(txt_files, 1):
                print(f"    {P}{i}{R}. {LV}{f.name}{R}")
            try:
                pick = int(_ask(f"\n{V}  ❯{R} Pick file: ")) - 1
                if not (0 <= pick < len(txt_files)):
                    print("Cancelled.")
                    return
                txt = txt_files[pick]
            except ValueError:
                print("Cancelled.")
                return
        else:
            txt = txt_files[0]
            print(f"  {LV}[i]{R} Using: {txt.name}")

        with open(txt, "r", encoding="utf-8") as f:
            urls = [l.strip() for l in f if l.strip() and "http" in l]
        if not urls:
            print(f"  {Y}[!]{R} No valid URLs in file.")
            return

        print(f"\n{_ts()} {LV}[i]{R} {len(urls)} URL(s) | {_fmt_quality_label(fmt, quality)}\n")
        _get_session_destination()
        _print_patience_banner()
        ok, fail = _batch_download_from_urls(urls, fmt, quality)
        print(f"  {G}[+]{R} Done! {ok} succeeded, {fail} failed. {_ts()}")

    elif mode == 3:
        fmt, quality = _get_fmt_quality()
        if not fmt: return
        print(f"\n  {LV}[i]{R} {_fmt_quality_label(fmt, quality)}\n")
        url = _ask(f"  {C}Playlist URL{R}: ").strip()
        if not url:
            print("Cancelled.")
            return
        _get_session_destination()
        _print_patience_banner()
        _dl_run_playlist(url, fmt, quality)
        _refresh_media_scan()
    elif mode == 4:
        _redownload_menu()
        _refresh_media_scan()
    else:
        print(f"  {Y}[!]{R} Invalid choice.")

    _finalize_downloaded_files()

_YT_META_FLAGS = [
    "--embed-metadata",
    "--parse-metadata", "%(title)s:%(meta_title)s",
    # NOTE on this artist chain: "artists"/"artist"/"creators" can legitimately
    # contain a whole comma-joined credits list (performers, writers,
    # producers -- whatever the source stuffed in there), not just the one
    # performing artist. Joining that straight into the ARTIST tag is what
    # produced tags like "Braaten, Aili, Salem Al Fakir, Vincent Pontare, ...".
    # First fix was to only ever keep the FIRST comma-separated name
    # (first_artist) -- but that was too aggressive the other way: real
    # featuring credits like "Little Mix, Jason Derulo" got chopped down to
    # just "Little Mix", losing the featured artist entirely.
    # Moderate fix: keep up to the first TWO comma/&-separated names instead
    # of just one. Two names covers the common "Main Artist, Featured Artist"
    # / "Main Artist & Featured Artist" case correctly, while still cutting
    # off long writer/producer credit dumps (3+ names) well before they'd
    # reach the tag -- not a perfect filter for every case, but a real
    # improvement over "always just the first name" without going back to
    # "keep everything". This also still collapses accidental back-to-back
    # duplicate names (e.g. "Broken Keo, Broken Keo") down to one, since a
    # duplicate second name is harmless to keep.
    "--parse-metadata", r"%(playlist_uploader,artists,artist,creators,uploader,channel,creator|)l:^(?P<uploader>.*?)(?:(?= - Topic)|$)",
    "--parse-metadata", r"%(uploader)s:^(?P<first_artist>[^,]+(?:\s*(?:,|&)\s*[^,]+)?)",
    "--parse-metadata", "%(first_artist,uploader|)s:%(artist)s",
    "--parse-metadata", "%(album,playlist_title,playlist,title|)s:%(meta_album)s",
    "--parse-metadata", "%(album_artist,first_artist,uploader|)s:%(album_artist)s",
    "--parse-metadata", r"%(release_year,release_date>%Y,upload_date>%Y)s:(?P<meta_date>\d+)",
    "--parse-metadata", r"%(description)s:(?P<meta_c>©[^\r\n]*)",
    "--parse-metadata", r"%(description)s:(?P<meta_p>℗[^\r\n]*)",
    "--parse-metadata", "%(meta_c|)s %(meta_p|)s:(?P<meta_copyright>.+)",
    "--parse-metadata", "%(composer|)s:%(meta_composer)s",
    "--parse-metadata", "%(genre|)s:%(meta_genre)s",
    "--parse-metadata", "%(track_number|)s:%(meta_track)s",
    "--parse-metadata", "%(disc_number|)s:%(meta_disc)s",
    "--embed-thumbnail",
    "--convert-thumbnails", "jpg",
    "--ppa", "ThumbnailsConvertor:-qmin 1 -q:v 1 -vf crop=ih:ih,scale=720:720",
    "--print", "after_move:TAGMAN_FILEPATH::%(filepath)s",
    "--print", "after_move:TAGMAN_THUMBNAIL::%(thumbnail)s",
]

def _get_fmt_quality():
    """Get (fmt, quality) for Sorcerer. Whatever's already remembered in
    Settings is used as-is; only the missing piece(s) get asked interactively —
    e.g. if only Format is remembered, it won't re-ask Format, just Quality."""
    fmt     = CONFIG.get("remember_format")
    quality = CONFIG.get("remember_quality")

    if not fmt:
        _sub_box("Format", [
            (1, "M4A",  "AAC, more compatible"),
            (2, "MP3",  "universal"),
            (3, "FLAC", "lossless, but not guarantee"),
            (4, "Opus", "efficient, small files"),
            (0, "Back", None),
        ], width=50, icon="♪")
        try:
            fc = int(_ask(f"\n{V}  ❯{R} Format: "))
        except ValueError:
            return None, None
        if fc == 0:
            return None, None
        if fc not in (1, 2, 3, 4):
            print(f"  {Y}[!]{R} Invalid choice.")
            return None, None
        fmt = {1: "m4a", 2: "mp3", 3: "flac", 4: "opus"}[fc]

    if fmt == "flac":
        # FLAC is lossless -- there's no bitrate to pick, so skip the prompt
        # entirely (any leftover "remember_quality" setting is irrelevant here).
        return fmt, "best"

    if not quality:
        _sub_box("Quality", [
            (1, "Default",                 "best by default (128kbps)"),
            (2, "160 kbps",                 "target bitrate 160kbps"),
            (3, "192 kbps",                 "target bitrate 192kbps"),
            (4, "256 kbps",                 "target bitrate 256kbps"),
            (5, "320 kbps",                 "target bitrate 320kbps"),
            (6, "968 kbps (experimental)",  "target bitrate 968kbps"),
            (0, "Back",                     None),
        ], width=54, icon="♪")
        try:
            qc = int(_ask(f"\n{V}  ❯{R} Quality: "))
        except ValueError:
            return None, None
        if qc == 0:
            return None, None
        if qc not in (1, 2, 3, 4, 5, 6):
            print(f"  {Y}[!]{R} Invalid choice.")
            return None, None
        quality = {1: "best", 2: "160", 3: "192", 4: "256", 5: "320", 6: "968"}[qc]

    return fmt, quality

# ─── Settings ────────────────────────────────────────────────────────────────

def _settings_pick_remember_format():
    _sub_box("Remember Format", [
        (1, "M4A",                       "AAC, more compatible"),
        (2, "MP3",                       "universal"),
        (3, "FLAC",                      "lossless, much bigger files"),
        (4, "Opus",                      "efficient, small files"),
        (5, "Don't remember",            "always ask on each download"),
        (0, "Back",                      None),
    ], width=54, icon="⚙")
    try:
        c = int(_ask(f"\n{V}  ❯{R} Pick: "))
    except ValueError:
        return
    mapping = {1: "m4a", 2: "mp3", 3: "flac", 4: "opus", 5: None}
    if c not in mapping:
        return
    CONFIG["remember_format"] = mapping[c]
    _save_config(CONFIG)
    print(f"  {G}[+]{R} Saved.")

def _settings_pick_remember_quality():
    _sub_box("Remember Quality", [
        (1, "Default (128kbps)",       None),
        (2, "160 kbps",                None),
        (3, "192 kbps",                None),
        (4, "256 kbps",                None),
        (5, "320 kbps",                None),
        (6, "968 kbps (experimental)", None),
        (7, "Don't remember",          "always ask on each download"),
        (0, "Back",                    None),
    ], width=54, icon="⚙")
    try:
        c = int(_ask(f"\n{V}  ❯{R} Pick: "))
    except ValueError:
        return
    mapping = {1: "best", 2: "160", 3: "192", 4: "256", 5: "320", 6: "968", 7: None}
    if c not in mapping:
        return
    CONFIG["remember_quality"] = mapping[c]
    _save_config(CONFIG)
    print(f"  {G}[+]{R} Saved.")

def _settings_preview_threshold():
    cur = CONFIG.get("preview_threshold", 7)
    print(f"\n  Current preview threshold: {cur}")
    new = input("  Enter new value (0 = always ask, empty to cancel): ").strip()
    if new == "":
        print("  Cancelled.")
        return
    try:
        val = int(new)
        if val < 0:
            print("  Must be >= 0.")
            return
        CONFIG["preview_threshold"] = val
        _save_config(CONFIG)
        print(f"  {G}[+]{R} Preview threshold set to {val}.")
    except ValueError:
        print("  Invalid number.")

def _settings_rename_mode():
    _sub_box("Folder Rename Mode", [
        (1, "Always ask",   "prompt after download"),
        (2, "Always do it", "automatically rename and revert without asking"),
        (3, "Do nothing",   "disable folder rename"),
        (0, "Back",         None),
    ], width=52, icon="⚙")
    try:
        c = int(_ask(f"\n{V}  ❯{R} Pick: "))
    except ValueError:
        return
    if c == 1:
        CONFIG["rename_mode"] = "ask"
    elif c == 2:
        CONFIG["rename_mode"] = "auto"
    elif c == 3:
        CONFIG["rename_mode"] = "off"
    else:
        return
    _save_config(CONFIG)
    print(f"  {G}[+]{R} Folder rename mode set to '{CONFIG['rename_mode']}'.")

def _settings_folder_reset_mode():
    _sub_box("Folder Reset Mode", [
        (1, "Always ask",  "cd back to $HOME once the task is done (every environment)"),
        (2, "Ignore",      "stay in the picked folder until you exit TagMan (default)"),
        (0, "Back",        None),
    ], width=60, icon="⚙")
    try:
        c = int(_ask(f"\n{V}  ❯{R} Pick: "))
    except ValueError:
        return
    if c == 1:
        CONFIG["folder_reset_mode"] = "always_ask"
    elif c == 2:
        CONFIG["folder_reset_mode"] = "ignore"
    else:
        return
    _save_config(CONFIG)
    print(f"  {G}[+]{R} Folder reset mode set to '{CONFIG['folder_reset_mode']}'.")

def _settings_video_search_mode():
    _sub_box("Video Search Mode", [
        (1, "Always ask",   "prompt once per search session"),
        (2, "Always yes",   "always search videos too (slower, more results)"),
        (3, "Never",        "songs-only search (old behavior, fastest)"),
        (0, "Back",         None),
    ], width=58, icon="⚙")
    try:
        c = int(_ask(f"\n{V}  ❯{R} Pick: "))
    except ValueError:
        return
    if c == 1:
        CONFIG["video_search_mode"] = "ask"
    elif c == 2:
        CONFIG["video_search_mode"] = "always"
    elif c == 3:
        CONFIG["video_search_mode"] = "never"
    else:
        return
    _save_config(CONFIG)
    print(f"  {G}[+]{R} Video search mode set to '{CONFIG['video_search_mode']}'.")

# ─── Cleaner (Settings sub-menu) ──────────────────────────────────────────

def _find_youtube_txt_files():
    """Scan the current working directory (non-recursive) for .txt files
    whose CONTENT contains the word 'youtube' (case-insensitive regex) --
    leftover URL-list/batch-queue files from Sorcerer that weren't cleaned
    up (e.g. a crash mid-download) or user-made batch .txt files left
    behind. Always uses Path.cwd(), which resolves to the real underlying
    directory even when you reached it through a symlink, so this behaves
    the same whether the folder was entered directly or via a symlinked
    path in bash."""
    import re
    hits = []
    for f in sorted(Path.cwd().glob("*.txt")):
        try:
            content = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if re.search(r"youtube", content, re.IGNORECASE):
            hits.append(f)
    return hits

def _find_stray_thumbnails():
    """Scan the current working directory (non-recursive) for loose image
    files -- leftover thumbnails from Sorcerer preview/download steps that
    never got embedded+deleted (e.g. after a cancelled preview or a crash).
    Always resolved against Path.cwd(), same reasoning as the .txt scan
    above."""
    exts = ("*.jpg", "*.jpeg", "*.png", "*.webp")
    hits = set()
    for pattern in exts:
        hits.update(Path.cwd().glob(pattern))
    return sorted(hits)

def _find_stray_json_files():
    """Scan the current working directory (non-recursive) for .json files --
    leftover metadata exports from Export/Import Metadata (*_meta.json) or
    any other stray .json left behind. Same non-recursive Path.cwd()
    reasoning as the .txt/thumbnail scans above.

    Excludes TagMan's own ".tagman_*.json" files (config, history) -- glob()
    matches dotfiles too (unlike shell globbing), so without this exclusion
    the Cleaner would offer to delete the config/history right alongside
    genuine stray exports whenever tagman.py's own folder is the cwd being
    cleaned."""
    return sorted(f for f in Path.cwd().glob("*.json") if not f.name.startswith(".tagman_"))

def _cleaner_do_delete(files, label):
    if not files:
        print(f"  {DIM}Nothing to clean -- no {label} found.{R}")
        return
    rows = [(f"  • {f.name}", f"  {DIM}•{R} {LV}{f.name}{R}") for f in files]
    _box(f"Found {len(files)} {label}", rows, min_width=46, icon="♻")
    konfirm = input(f"\n  Delete all {len(files)} file(s)? (Y/n): ").strip().lower()
    if konfirm == "n":
        print(f"  {DIM}Cancelled.{R}")
        return
    print(f"  {_ts()} {DIM}Cleaning {label} — starting...{R}")
    ok = 0
    for f in files:
        try:
            f.unlink()
            ok += 1
        except Exception as e:
            print(f"  {Y}[!]{R} Failed to delete {f.name}: {e}")
    print(f"  {G}[+]{R} Deleted {ok}/{len(files)} file(s). {_ts()}")

def _cleaner_menu():
    """Settings sub-menu 6: sweep leftover 'youtube' .txt files, stray
    thumbnail images, and stray .json metadata exports out of the current
    folder."""
    while True:
        txts   = _find_youtube_txt_files()
        thumbs = _find_stray_thumbnails()
        jsons  = _find_stray_json_files()

        _sub_box(f"Cleaner — {Path.cwd().name}", [
            (1, f"Clean .txt files",         f"contains \"youtube\" — {len(txts)} found"),
            (2, f"Clean stray thumbnails",   f".jpg/.jpeg/.png/.webp — {len(thumbs)} found"),
            (3, f"Clean stray .json",        f".json — {len(jsons)} found"),
            (4, f"Clean all",                None),
            (0, "Back",                      None),
        ], width=56, icon="♻")

        try:
            c = int(_ask(f"\n{V}  ❯{R} Pick: "))
        except ValueError:
            return
        if c == 0:
            return
        elif c == 1:
            _cleaner_do_delete(txts, ".txt file(s)")
        elif c == 2:
            _cleaner_do_delete(thumbs, "image file(s)")
        elif c == 3:
            _cleaner_do_delete(jsons, ".json file(s)")
        elif c == 4:
            _cleaner_do_delete(txts, ".txt file(s)")
            _cleaner_do_delete(thumbs, "image file(s)")
            _cleaner_do_delete(jsons, ".json file(s)")
        else:
            print(f"  {Y}[!]{R} Invalid choice.")

# ─── Folder Shortcut (interactive picker) ──────────────────────────────────
# Lets the user browse folders with the arrow keys, interactively, and drop a
# launcher symlink into whichever one they land on. TagMan operates on the
# shell's current working directory, not the symlink's location, so running
# that symlink from inside the target folder starts TagMan pointed at it.

def _read_key():
    """Read a single keypress (including arrow keys) without waiting for
    Enter. Returns 'up'/'down'/'left'/'right'/'enter'/'backspace'/'q', a
    single printable character, or '' if nothing usable was read."""
    if IS_WINDOWS:
        try:
            import msvcrt
        except Exception:
            return ''
        ch = msvcrt.getch()
        if ch in (b'\x00', b'\xe0'):  # arrow-key prefix on Windows
            ch2 = msvcrt.getch()
            return {b'H': 'up', b'P': 'down', b'K': 'left', b'M': 'right'}.get(ch2, '')
        if ch in (b'\r', b'\n'):
            return 'enter'
        if ch == b'\x08':
            return 'backspace'
        if ch in (b'q', b'Q', b'\x1b', b'\x03', b'\x04'):  # q/Q, Esc, Ctrl+C, Ctrl+D
            return 'q'
        try:
            return ch.decode('utf-8', 'ignore')
        except Exception:
            return ''
    else:
        try:
            import termios, tty
        except Exception:
            return ''
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == '\x1b':
                ch2 = sys.stdin.read(1)
                if ch2 == '[':
                    ch3 = sys.stdin.read(1)
                    return {'A': 'up', 'B': 'down', 'C': 'right', 'D': 'left'}.get(ch3, '')
                return 'q'  # bare Esc
            if ch in ('\r', '\n'):
                return 'enter'
            if ch in ('\x7f', '\x08'):
                return 'backspace'
            if ch in ('q', 'Q', '\x03', '\x04'):  # q/Q, Ctrl+C (ETX), Ctrl+D (EOT)
                return 'q'
            return ch
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

def _list_subdirs(path):
    """Sorted, visible (non-hidden) subdirectories of `path`."""
    try:
        items = [p for p in path.iterdir() if p.is_dir() and not p.name.startswith(".")]
        items.sort(key=lambda p: p.name.lower())
        return items
    except Exception:
        return []

def _pick_folder_interactive(start):
    """Interactive folder browser.
    ↑/↓ move, Enter opens the highlighted folder (or '..' to go up),
    ←/Backspace also goes up a level, 's' selects the CURRENTLY OPEN folder,
    'q'/Esc cancels. Returns a Path or None if cancelled."""
    current = start
    idx = 0
    while True:
        entries = _list_subdirs(current)
        names = ([".."] if current != current.parent else []) + [p.name for p in entries]
        if not names:
            names = ["(no subfolders)"]
        idx = max(0, min(idx, len(names) - 1))

        os.system("clear")
        cap = max(30, _term_width() - 4)
        width = min(60, cap)
        print(f"\n{V}  ╔═ ✦ Choose Folder {'═' * max(0, width - 18)}╗{R}")
        shown = _fit_name(str(current), maxlen=width - 2)
        pad0 = max(0, width - _vlen(shown))
        print(f"{V}  ║{R} {DIM}{shown}{R}{' ' * pad0}{V}║{R}")
        print(f"{V}  ╟{'─' * width}╢{R}")
        for i, name in enumerate(names):
            marker = f"{P}❯{R}" if i == idx else " "
            color = LV if i == idx else DIM
            label = _fit_name(name, maxlen=width - 4)
            plain = f" {name}"
            pad = max(0, width - _vlen(plain))
            print(f"{V}  ║{R}{marker} {color}{label}{R}{' ' * pad}{V}║{R}")
        print(f"{V}  ╚{'═' * width}╝{R}")
        print(f"\n  {DIM}↑/↓ move   Enter open   ←/Backspace up   s select this folder   q cancel{R}")

        key = _read_key()
        if key == 'up':
            idx -= 1
        elif key == 'down':
            idx += 1
        elif key == 'q':
            return None
        elif key in ('backspace', 'left'):
            if current != current.parent:
                current = current.parent
                idx = 0
        elif key in ('enter', 'right'):
            sel = names[idx]
            if sel == "..":
                if current != current.parent:
                    current = current.parent
                    idx = 0
            elif sel != "(no subfolders)":
                current = current / sel
                idx = 0
        elif key == 's':
            return current
        # anything else: ignore and redraw

def _launcher_source_path():
    """Locate the real TagMan launcher to point new folder shortcuts at.
    Prefers a 'tagman.sh' wrapper sitting next to tagman.py (the launcher
    Termux users normally run); falls back to tagman.py itself if no
    wrapper exists."""
    try:
        here = Path(__file__).resolve().parent
        sh = here / "tagman.sh"
        if sh.exists():
            return sh
        return Path(__file__).resolve()
    except Exception:
        return None

def _launcher_ref(src):
    """How a generated wrapper should refer to `src`. If it sits directly in
    the user's home directory (the normal case — that's where tagman.py and
    tagman.sh live), use '$HOME/name' ('%USERPROFILE%\\name' on Windows) so
    the wrapper keeps working even if $HOME/the home drive letter differs
    between the machine that made the shortcut and wherever it ends up
    running. Otherwise (TagMan was actually run from some other folder —
    e.g. a copy sitting elsewhere that got cd'd into) fall back to the
    literal absolute path that was detected, since there's no portable
    shorthand for an arbitrary folder."""
    try:
        home = Path.home().resolve()
        resolved = src.resolve()
        if resolved.parent == home:
            return f"%USERPROFILE%\\{resolved.name}" if IS_WINDOWS else f"$HOME/{resolved.name}"
        return str(resolved)
    except Exception:
        return str(src)

def _write_launcher_wrapper(link_path, src):
    """Write a small wrapper that execs TagMan's real script. Every launcher
    shortcut TagMan creates is now one of these — a plain bash script on
    POSIX (Termux/Linux/macOS), a .bat on Windows — instead of a raw
    symlink, so shortcuts behave identically everywhere regardless of
    whether the target filesystem can actually hold a symlink."""
    ref = _launcher_ref(src)
    if IS_WINDOWS:
        if src.suffix == ".py":
            body = f'@echo off\r\npython "{ref}" %*\r\n'
        else:
            body = f'@echo off\r\ncall "{ref}" %*\r\n'
        link_path.write_text(body, encoding="utf-8")
        return

    bash = '/data/data/com.termux/files/usr/bin/bash' if IS_TERMUX else '/bin/bash'
    if src.suffix == ".py":
        body = f'#!{bash}\nexec python3 "{ref}" "$@"\n'
    else:
        # src is already a launcher script (e.g. tagman.sh) — just relay to it.
        body = f'#!{bash}\nexec "{ref}" "$@"\n'
    link_path.write_text(body, encoding="utf-8")

# ─── Move Downloads (pick destination before downloading starts) ──────────
# Downloads always land in cwd first — yt-dlp needs a real write target and
# guessing/auto-binding a destination up front just meant an extra silent
# cd. The destination folder itself, though, is now picked *before* the
# actual download kicks off — right after any thumbnail preview and right
# before the "download this? (Y/n)" style confirmation — using the same
# interactive browser used by Settings -> Folder Shortcut. It's asked once
# per Sorcerer session (cached in _SESSION_DEST_FOLDER) so declining one
# song and picking another doesn't re-prompt. Every file actually
# downloaded in that session is tracked in _SESSION_DOWNLOADED_FILES; once
# downloader() is done, if anything downloaded and a destination other than
# cwd was picked, the file(s) get moved there.

_SESSION_DOWNLOADED_FILES = []
_SESSION_DEST_FOLDER = None
_SESSION_DEST_ASKED  = False

def _reset_session_destination():
    global _SESSION_DEST_FOLDER, _SESSION_DEST_ASKED
    _SESSION_DEST_FOLDER = None
    _SESSION_DEST_ASKED  = False

def _maybe_reset_session_destination():
    """Resets the remembered Sorcerer download destination according to the
    same 'folder_reset_mode' Settings choice every other folder pick in
    TagMan follows:
      - "always_ask": forget it, so every new Sorcerer visit re-prompts.
      - "ignore" (default): keep reusing whatever was picked last. Sorcerer
        tracks its destination separately from the general scan-folder cd
        (see _maybe_pick_scan_folder/_apply_folder_reset_policy above),
        so without this it kept re-asking on every single Sorcerer visit
        even with 'ignore' selected -- the destination equivalent of the
        cd bug those two fix for everything else."""
    if CONFIG.get("folder_reset_mode", "ignore") == "always_ask":
        _reset_session_destination()

def _get_session_destination():
    """Ask once per Sorcerer session where the downloaded file(s) should end
    up, using the same interactive folder browser as Settings -> Folder
    Shortcut. Cancelling (q) or picking the current folder means "leave it
    in cwd" — returns None either way. Cached after the first call so a
    Search & Download loop that declines a few songs before finally
    downloading one doesn't get asked again.

    Skipped entirely when cwd is already NOT $HOME -- same reasoning as
    _maybe_pick_scan_folder(): if you're already sitting in the folder you
    want (e.g. you launched TagMan via a launcher symlink/wrapper placed
    inside that folder), that folder IS the destination, and asking again is
    just noise. This only used to trigger from $HOME, where cwd genuinely
    doesn't tell you anything about where the download should land."""
    global _SESSION_DEST_FOLDER, _SESSION_DEST_ASKED
    if _SESSION_DEST_ASKED:
        return _SESSION_DEST_FOLDER
    _SESSION_DEST_ASKED = True

    try:
        cwd_is_home = Path.cwd().resolve() == Path.home().resolve()
    except Exception:
        cwd_is_home = False
    if not cwd_is_home:
        _SESSION_DEST_FOLDER = None
        return None

    print(f"\n  {LV}[i]{R} Pick a folder to save the downloaded file(s) into ({P}q{R} to keep them in the current folder).")
    if IS_TERMUX and (Path.home() / "storage" / "shared").exists():
        start = Path.home() / "storage" / "shared"
    else:
        start = Path.home()

    dest = _pick_folder_interactive(start)
    os.system("clear")
    if dest is None:
        _SESSION_DEST_FOLDER = None
        print(f"  {DIM}[i] Keeping downloads in the current folder.{R}")
        return None

    try:
        same_folder = dest.resolve() == Path.cwd().resolve()
    except Exception:
        same_folder = False
    if same_folder:
        _SESSION_DEST_FOLDER = None
        print(f"  {DIM}[i] Keeping downloads in the current folder.{R}")
        return None

    _SESSION_DEST_FOLDER = dest
    print(f"  {G}[+]{R} Saving downloaded file(s) to: {LV}{dest}{R}")
    return dest

def _finalize_downloaded_files():
    """Called at the end of downloader(). Moves every file downloaded this
    session into the destination picked earlier via _get_session_destination
    (if any — a destination of "here" means nothing to do). Reports plain
    success/failure rather than a raw moved-count line.

    Also cd's into that destination once the move is done. Without this, cwd
    stayed wherever it was before the download (usually $HOME) even though
    the files themselves landed in the picked folder — so
    'folder_reset_mode: ignore' had nothing to actually stay put *in*, and
    the very next scan (Lyrics, Check Tags, ...) saw cwd still at $HOME and
    asked to pick a folder all over again, right after you'd just picked
    one. Chdir'ing here gives _apply_folder_reset_policy() (which runs right
    after this, once the whole action is done) the same single cwd to work
    from as every other folder-picking feature: 'ignore' leaves you sitting
    in the destination, 'always_ask' cd's back to $HOME as before."""
    files = [f for f in _SESSION_DOWNLOADED_FILES if f.exists()]
    dest  = _SESSION_DEST_FOLDER
    _SESSION_DOWNLOADED_FILES.clear()
    _maybe_reset_session_destination()
    if not files or dest is None:
        return

    moved = 0
    for f in files:
        try:
            target = dest / f.name
            if target.exists():
                # Clean, human-readable dedup: "Title.ext" -> "Title (2).ext",
                # "Title (3).ext", ... instead of a raw epoch timestamp (which
                # produced unreadable names like "Babydoll_1784538637.opus").
                n = 2
                while True:
                    candidate = dest / f"{f.stem} ({n}){f.suffix}"
                    if not candidate.exists():
                        target = candidate
                        break
                    n += 1
            shutil.move(str(f), str(target))
            moved += 1
        except Exception:
            pass

    if moved == len(files):
        print(f"  {G}[+]{R} Success — saved to {LV}{dest}{R}")
    else:
        print(f"  {Y}[!]{R} Unsuccessful — couldn't move all file(s) to {LV}{dest}{R}")

    if moved:
        try:
            os.chdir(dest)
        except Exception:
            pass

def _create_folder_shortcut():
    """Settings sub-feature: interactive folder browser + launcher shortcut.
    Starts at /sdcard on Termux/Android, $HOME everywhere else (per the
    same platform split used across the rest of TagMan). The resulting
    shortcut runs TagMan with whatever folder you launch it *from* as the
    working directory — so cd into the target folder and run it there.
    Always a bash wrapper script on POSIX (tagman.sh) / .bat on Windows —
    no raw symlinks, so it behaves identically on every filesystem
    (including Android's /sdcard, which can't hold symlinks at all)."""
    start = Path("/sdcard") if (IS_TERMUX and Path("/sdcard").exists()) else Path.home()

    print(f"\n  {LV}[i]{R} Browse to a folder, then press {P}s{R} to select it (q to cancel).")
    target_dir = _pick_folder_interactive(start)
    os.system("clear")
    if target_dir is None:
        print(f"  {DIM}Cancelled — no folder selected.{R}")
        return

    src = _launcher_source_path()
    if not src or not src.exists():
        print(f"  {Y}[!]{R} Could not locate TagMan's own script to link to.")
        return

    default_name = "tagman.bat" if IS_WINDOWS else "tagman.sh"

    print(f"\n  {LV}[i]{R} Selected: {DIM}{target_dir}{R}")
    name = input(f"  {V}❯{R} Shortcut name [{default_name}]: ").strip() or default_name
    link_path = target_dir / name

    if link_path.exists() or link_path.is_symlink():
        ow = input(f"  {Y}[!]{R} '{name}' already exists there. Overwrite? (y/N): ").strip().lower()
        if ow != "y":
            print(f"  {DIM}Cancelled.{R}")
            return
        try:
            link_path.unlink()
        except Exception as e:
            print(f"  {Y}[!]{R} Could not remove existing file: {e}")
            return

    try:
        _write_launcher_wrapper(link_path, src)
        try:
            os.chmod(str(link_path), 0o755)
        except Exception:
            pass

        print(f"\n  {G}[+]{R} Shortcut created (shell script): {LV}{link_path}{R}")
        print(f"  {DIM}→ runs {src}{R}")

        if IS_WINDOWS:
            # cmd/PowerShell run .bat files by name (or .\name) without
            # needing an executable bit — there isn't one on Windows.
            print(f"  {DIM}cd into that folder and run {name} (or .\\{name} in PowerShell) to launch TagMan there.{R}")
        else:
            # FUSE storage (Android /sdcard) silently ignores chmod, so the
            # exec bit never actually sticks even though chmod didn't error.
            runnable = os.access(str(link_path), os.X_OK)
            if runnable:
                print(f"  {DIM}cd into that folder and run ./{name} to launch TagMan there.{R}")
            else:
                print(f"  {Y}[!]{R} This filesystem won't keep the executable bit set (common on /sdcard),")
                print(f"  {DIM}so run it with: bash {name}   (cd into the folder first){R}")
    except Exception as e:
        print(f"  {Y}[!]{R} Failed to create shortcut: {e}")


_HISTORY_TYPE_ICON = {"download": "⇣", "edit": "✎", "lyrics": "♪", "cover": "◆"}

def _history_show(entries):
    if not entries:
        print(f"\n  {DIM}Nothing to show.{R}")
        return
    rows = []
    for e in entries:
        icon = _HISTORY_TYPE_ICON.get(e.get("type"), "•")
        rows.append((
            f"  {icon} [{e.get('timestamp', '?')}] {e.get('type', '?').upper()}",
            f"  {icon} {DIM}[{e.get('timestamp', '?')}]{R} {LV}{e.get('type', '?').upper()}{R}"
        ))
        action = e.get("action", "")
        rows.append((f"    {action}", f"    {action}"))
        if e.get("file"):
            rows.append((f"    File: {e['file']}", f"    {DIM}File: {e['file']}{R}"))
        if e.get("url"):
            rows.append((f"    URL: {e['url']}", f"    {DIM}URL: {e['url']}{R}"))
    _box("History", rows, min_width=55, max_width=76, icon="⏱")

def _history_menu():
    """Settings -> History. Only ever shows entries logged from the CURRENT
    folder (matched by absolute path) -- see _history_for_this_folder()."""
    while True:
        entries = list(reversed(_history_for_this_folder()))  # newest first
        if not entries:
            print(f"\n  {Y}[!]{R} No history recorded for this folder yet.")
            print(f"  {DIM}{Path.cwd()}{R}")
            return

        _sub_box("History", [
            (1, "View recent",              "last 25 entries for this folder"),
            (2, "Filter by type",           "download / edit / lyrics / cover"),
            (3, "Clear this folder's history", None),
            (0, "Back",                     None),
        ], width=55, icon="⏱")
        try:
            c = int(_ask(f"\n{V}  ❯{R} Pick: "))
        except ValueError:
            return
        if c == 0:
            return
        elif c == 1:
            _history_show(entries[:25])
        elif c == 2:
            types = sorted({e.get("type", "?") for e in entries})
            rows = [(f"  {i}. {t}", f"  {P}{i}{R}. {LV}{t}{R}") for i, t in enumerate(types, 1)]
            rows.append((f"   0. Back", f"   {P}0{R}. {DIM}Back{R}"))
            _box("Pick Type", rows, min_width=40, icon="☰")
            try:
                tpick = int(_ask(f"\n{V}  ❯{R} Pick: "))
            except ValueError:
                continue
            if tpick == 0 or not (1 <= tpick <= len(types)):
                continue
            wanted = types[tpick - 1]
            _history_show([e for e in entries if e.get("type") == wanted][:50])
        elif c == 3:
            konfirm = input(f"\n  Delete ALL history entries for this folder? This can't be undone. (y/N): ").strip().lower()
            if konfirm == "y":
                all_entries = _history_load_all()
                here = str(Path.cwd().resolve())
                before = len(all_entries)
                kept = [e for e in all_entries if e.get("folder") != here]
                if _history_save_all(kept):
                    print(f"  {G}[+]{R} Cleared {before - len(kept)} entr{'y' if before - len(kept) == 1 else 'ies'} for this folder.")
                else:
                    print(f"  {Y}[!]{R} Failed to save.")
        else:
            print(f"  {Y}[!]{R} Invalid choice.")

def settings_menu():
    """Menu 8: view & edit TagMan config. Can also edit JSON file manually."""
    while True:
        fmt_val   = CONFIG.get("remember_format")
        fmt_label = fmt_val.upper() if fmt_val else "Not set (always ask)"
        q_val     = CONFIG.get("remember_quality")
        q_label   = {"best": "Default (128kbps)", "160": "160 kbps", "192": "192 kbps",
                     "256": "256 kbps", "320": "320 kbps",
                     "968": "968 kbps (experimental)"}.get(q_val, "Not set (always ask)")
        thresh    = CONFIG.get("preview_threshold", 7)
        rename    = CONFIG.get("rename_mode", "ask")
        vid_mode  = CONFIG.get("video_search_mode", "ask")
        fold_mode = CONFIG.get("folder_reset_mode", "ignore")

        _sub_box("Settings", [
            (1, f"Remember Format: {fmt_label}",        "skip format prompt in Sorcerer"),
            (2, f"Remember Quality: {q_label}",         "skip quality prompt in Sorcerer"),
            (3, f"Preview Threshold: {thresh}",          "how many thumbnails to preview before skipping"),
            (4, f"Folder Rename Mode: {rename}",         "'ask'/'auto'/'off' — Termux/Android only, no-op elsewhere"),
            (5, "Config file location",                  "for manual editing with a text editor"),
            (6, "Cleaner",                                "sweep leftover 'youtube' .txt, thumbnails & .json from cwd"),
            (7, f"Video Search Mode: {vid_mode}",         "'ask', 'always', or 'never' search videos too"),
            (8, "Folder Shortcut",                        "browse (interactive) & drop a launcher shortcut into a folder"),
            (9, f"Folder Reset Mode: {fold_mode}",        "'always_ask' (return to $HOME after each task) or 'ignore' (stay put)"),
            (10, "History",                               "view edit/download history for this folder"),
            (0, "Back",                                  None),
        ], width=60, icon="⚙")

        try:
            c = int(_ask(f"\n{V}  ❯{R} Pick: "))
        except ValueError:
            return
        if c == 0:
            return
        elif c == 1:
            _settings_pick_remember_format()
        elif c == 2:
            _settings_pick_remember_quality()
        elif c == 3:
            _settings_preview_threshold()
        elif c == 4:
            _settings_rename_mode()
        elif c == 5:
            print(f"\n  {LV}[i]{R} Config stored at:")
            print(f"  {DIM}{CONFIG_PATH}{R}")
            print(f"  {DIM}Plain JSON file, can be opened/edited with any text editor.{R}")
        elif c == 6:
            _cleaner_menu()
        elif c == 7:
            _settings_video_search_mode()
        elif c == 8:
            _create_folder_shortcut()
        elif c == 9:
            _settings_folder_reset_mode()
        elif c == 10:
            _history_menu()
        else:
            print(f"  {Y}[!]{R} Invalid choice.")

# ─── Media scan Issue Helper ─────────────────────────────────────────────

def _rename_folder_tmp():
    """Rename current working directory to folder_tmp (only once)."""
    try:
        cwd = Path.cwd()
        if not cwd.exists():
            return
        parent_str = str(cwd)
        if parent_str in ['/', '/sdcard', '/storage/emulated/0', '/storage/emulated']:
            return
        orig_name = cwd.name
        if not orig_name:
            return
        if orig_name.endswith("_tmp"):
            return
        tmp_name = orig_name + "_tmp"
        tmp_path = cwd.with_name(tmp_name)
        cwd.rename(tmp_path)
        print(f"  {DIM}[~] Folder temporarily renamed: {orig_name} → {tmp_name}{R}")
    except Exception as e:
        print(f"  {Y}[!]{R} Failed to rename folder: {e}")

def _ask_rename_folder_back():
    """Ask user to rename folder back from _tmp to original."""
    try:
        cwd = Path.cwd()
        if not cwd.exists():
            return
        parent_str = str(cwd)
        if parent_str in ['/', '/sdcard', '/storage/emulated/0', '/storage/emulated']:
            return
        orig_name = cwd.name
        if not orig_name.endswith("_tmp"):
            return
        original = orig_name[:-4]
        print(f"\n  Current folder: {orig_name} (was {original})")
        konfirm = input(f"  Rename back to {original}? (Y/n): ").strip().lower()
        if konfirm != "n":
            new_path = cwd.with_name(original)
            cwd.rename(new_path)
            print(f"  {G}[+]{R} Folder restored to {original}")
        else:
            print(f"  {DIM}Folder kept as {orig_name}{R}")
    except Exception as e:
        print(f"  {Y}[!]{R} Failed to rename folder: {e}")

def _rename_folder_back_auto():
    """Automatically rename back from _tmp to original without asking."""
    try:
        cwd = Path.cwd()
        if not cwd.exists():
            return
        parent_str = str(cwd)
        if parent_str in ['/', '/sdcard', '/storage/emulated/0', '/storage/emulated']:
            return
        orig_name = cwd.name
        if not orig_name.endswith("_tmp"):
            return
        original = orig_name[:-4]
        new_path = cwd.with_name(original)
        cwd.rename(new_path)
        print(f"  {DIM}[~] Folder automatically restored to {original}{R}")
    except Exception as e:
        print(f"  {Y}[!]{R} Failed to auto-rename folder: {e}")

def _refresh_media_scan():
    """Force Android to re-index files that were just modified, so file
    managers/players stop showing stale cached thumbnails/tags. Uses the
    folder-rename trick (rename to *_tmp then back), according to config mode.
    This is an Android/MediaStore quirk only — Linux/macOS/Windows file
    managers and players read fresh file state directly, so this is skipped
    there entirely."""
    if not IS_TERMUX:
        return
    mode = CONFIG.get("rename_mode", "ask")
    if mode == "off":
        return
    _rename_folder_tmp()
    if mode == "auto":
        _rename_folder_back_auto()
    else:  # "ask"
        _ask_rename_folder_back()

def main():
    print(f"{C}  ♪ ♫ ♪ ♫ ♪ ♫ ♪ ♫ ♪ ♫ ♪ ♫ ♪ ♫ ♪ ♫ ♪ ♫ ♪ ♫ ♪ ♫{R}")
    print(ASCII_BANNER)
    print(f"{C}  ♪ ♫ ♪ ♫ ♪ ♫ ♪ ♫ ♪ ♫ ♪ ♫ ♪ ♫ ♪ ♫ ♪ ♫ ♪ ♫ ♪ ♫{R}\n")
    print(f"  {DIM}[i] Environment detected: {ENV}{R}")
    if IS_WINDOWS:
        print(f"  {Y}[!] Windows support is beta/experimental — image preview and some")
        print(f"      file operations may behave differently than on Termux/Linux.{R}")
    while True:
        _apply_folder_reset_policy()
        _print_menu()

        try:
            choice = int(_ask(f"\n{V}  ❯{R} Pick menu: "))
        except (ValueError, EOFError):
            continue
        except _ToMainMenu:
            continue

        if choice == 0:
            print(f"\n{C}  ♪ ♫ ♪ ♫ ♪ ♫ ♪ ♫ ♪ ♫ ♪ ♫ ♪ ♫ ♪ ♫ ♪ ♫ ♪ ♫ ♪ ♫{R}")
            print(EXIT_BANNER)
            print(f"{C}  ♪ ♫ ♪ ♫ ♪ ♫ ♪ ♫ ♪ ♫ ♪ ♫ ♪ ♫ ♪ ♫ ♪ ♫ ♪ ♫ ♪ ♫{R}\n")
            break

        if choice == 3993:
            _stay()
            continue

        if choice == 33333:
            try:
                _secret_comment_menu()
            except _ToMainMenu:
                pass
            os.system("clear")
            continue

        if choice not in range(1, len(MENU) + 1):
            print(f"{Y}  [!]{R} Invalid choice.")
            continue

        try:
            if choice == 6:
                meta_menu()
                continue

            if choice == 7:
                downloader()
                continue

            if choice == 8:
                run_validation()
                continue

            if choice == 9:
                settings_menu()
                continue

            if choice == 4:
                tags_editor()
                continue

            if choice in (2, 5):
                if choice == 2:
                    _sub_box("Insert Cover", [
                        (1, "Manual",                      "pick one image file"),
                        (2, "Batch",                        "match song names ↔ images in folder"),
                        (3, "Download YouTube",              "thumbnail from URL / .txt"),
                        (4, "Fetch from YTMusic (Batch)",    "all songs with blacklist"),
                        (5, "Fetch from YTMusic (Single)",   "pick one song"),
                        (0, "Back", None),
                    ], width=55, icon="◆")
                    try:
                        sub = int(_ask(f"\n{V}  ❯{R} Pick: "))
                    except ValueError:
                        print(f"  {Y}[!]{R} Invalid choice.")
                        continue
                    if sub == 0:
                        continue
                    elif sub == 1:
                        fps = pick_files()
                        for fp in fps:
                            insert_cover(fp, _manual=True)
                    elif sub == 2:
                        insert_cover(None, _manual=False)
                    elif sub == 3:
                        download_thumbnail()
                    elif sub == 4:
                        fetch_thumbnails_from_ytmusic()
                    elif sub == 5:
                        fetch_single_thumbnail_from_ytmusic()
                    else:
                        print(f"  {Y}[!]{R} Invalid choice.")
                else:
                    add_lyrics()
                continue

            # For choices 1 and 3, use multi file selection
            fps = pick_files()
            if not fps:
                continue
            if choice == 1:
                for fp in fps:
                    extract_cover(fp)
            elif choice == 3:
                for fp in fps:
                    check_tags(fp)
        except _ToMainMenu:
            print(f"\n  {DIM}↺ Back to main menu.{R}")
            continue

if __name__ == "__main__":
    main()