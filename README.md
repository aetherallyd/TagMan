# TagMan

**TagMan is a tool for tools.**

It isn't a standalone product — it's a terminal-based manager and downloader
that wraps a handful of already-excellent open-source tools (`yt-dlp`,
`mutagen`, `ytmusicapi`, `syncedlyrics`, `ffmpeg`, `Pillow`) into one
interactive menu, so you don't have to remember flags, write scripts, or
juggle five separate CLIs just to grab a song, embed its cover art, fix its
tags, and fetch synced lyrics.

Everything TagMan does, under the hood, is one of those tools doing the
actual work. TagMan's job is just to be the friendly front-of-house for
them — extract/embed cover art, check and edit metadata, fetch and embed
lyrics, export/import tag data, search & download from YouTube Music, and
so on — all from a menu you can drive with arrow keys on a phone screen.

## What it runs on

Built and tested primarily on **Termux (Android)**. It also runs on
**Linux** and **macOS**. See `install.sh`.

### A note on Windows

TagMan has a Windows code path (it'll generate `.bat` wrappers, etc.), but
this is **not guaranteed to run smoothly**. Windows support is
experimental/beta at best — some features (image preview, certain
filesystem tricks) don't have a clean native Windows equivalent yet.

For now, if you're on Windows, the most reliable route is **WSL**
(Windows Subsystem for Linux):

```powershell
wsl --install
```

Then open a WSL/Ubuntu terminal and follow the Linux install steps below.
Native Windows (cmd/PowerShell without WSL) may work partially, but expect
rough edges.

## Install

```bash
git clone https://github.com/aetherallyd/TagMan.git
cd TagMan
./install.sh
```

Or one-liner:

```bash
curl -sL https://raw.githubusercontent.com/aetherallyd/TagMan/main/install.sh | bash
```

This installs `git`, `python`, and `ffmpeg` for your platform (Termux /
Linux / macOS — see the WSL note above for Windows), installs the Python
dependencies from `requirements.txt`, and sets up a `tagman.sh` launcher.

Run it with:

```bash
./tagman.sh
```

## Useful tip
If you ever feel like your storage got bulge just after you installed the tool.
Well it's because you run it on a fresh termux (if Android), if that happens, it's normal.
Run this on your terminal if you feel you wouldn't need these dependencies, because you can uninstall it since TagMan doesn't use those:

```bash
pkg uninstall -y clang make binutils rust python-dev && apt autoremove -y && pkg clean
```

## Credits

TagMan stands on the shoulders of the people and projects that actually
make it work:

- **[yt-dlp](https://github.com/yt-dlp/yt-dlp)** — the foundation TagMan is
  built on. Without it, TagMan simply wouldn't exist.
- **[deniscerri](https://github.com/deniscerri)**, for
  [yt-dlnis](https://github.com/deniscerri/ytdlnis) — the inspiration for
  building something that makes `yt-dlp` easier to actually use.
- **& everyone else** behind the open-source libraries TagMan relies on:
  `mutagen`, `Pillow`, `ytmusicapi`, `syncedlyrics`, and more.


# Just confession

And I have to confess that yes... I, I mean we created this tool.
I used LLM to work with me created this script... that's the best way to use LLM nowadays.