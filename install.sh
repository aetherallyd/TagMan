#!/usr/bin/env bash
# install.sh — one-shot installer for TagMan
# Usage:
#   curl -sL https://raw.githubusercontent.com/USERNAME/tagman/main/install.sh | bash
# (replace USERNAME/tagman with your actual GitHub repo)

set -e

REPO_URL="https://github.com/USERNAME/tagman.git"   # <-- change this
INSTALL_DIR="$HOME/tagman"

echo "==> Detecting environment..."
if [ -n "$TERMUX_VERSION" ] || [[ "$PREFIX" == *com.termux* ]]; then
    ENV="termux"
elif [ "$(uname)" = "Darwin" ]; then
    ENV="macos"
elif grep -qi microsoft /proc/version 2>/dev/null; then
    ENV="linux"   # WSL — behaves like regular Linux from here on
elif [[ "$(uname -s)" == MINGW* || "$(uname -s)" == MSYS* || "$(uname -s)" == CYGWIN* ]]; then
    echo ""
    echo "    Native Windows (Git Bash/MSYS/Cygwin) isn't supported by this"
    echo "    installer yet. For now, install WSL and run this script inside"
    echo "    it instead:"
    echo ""
    echo "      wsl --install"
    echo ""
    echo "    Then reopen a WSL/Ubuntu terminal and re-run this installer there."
    exit 1
else
    ENV="linux"
fi
echo "    -> $ENV"

echo "==> Installing system dependencies..."
case "$ENV" in
    termux)
        pkg update -y
        pkg install -y git python ffmpeg
        ;;
    macos)
        if ! command -v brew >/dev/null 2>&1; then
            echo "    Homebrew not found — install it first: https://brew.sh"
            exit 1
        fi
        brew install git python ffmpeg
        ;;
    linux)
        if command -v apt >/dev/null 2>&1; then
            sudo apt update
            sudo apt install -y git python3 python3-pip ffmpeg
        elif command -v pacman >/dev/null 2>&1; then
            sudo pacman -Sy --noconfirm git python python-pip ffmpeg
        elif command -v dnf >/dev/null 2>&1; then
            sudo dnf install -y git python3 python3-pip ffmpeg
        else
            echo "    Unrecognized package manager — install git, python3, pip, and ffmpeg manually."
        fi
        ;;
esac

echo "==> Cloning TagMan into $INSTALL_DIR..."
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "    Already cloned — pulling latest instead."
    git -C "$INSTALL_DIR" pull
else
    git clone "$REPO_URL" "$INSTALL_DIR"
fi

echo "==> Installing Python dependencies..."
cd "$INSTALL_DIR"
if [ "$ENV" = "termux" ]; then
    pip install -r requirements.txt
else
    pip3 install -r requirements.txt --break-system-packages 2>/dev/null \
        || pip3 install -r requirements.txt
fi

echo "==> Creating launcher (tagman.sh)..."
cat > "$INSTALL_DIR/tagman.sh" <<'EOF'
#!/bin/bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$DIR/tagman.py" "$@"
EOF
chmod +x "$INSTALL_DIR/tagman.sh"

echo ""
echo "==> Done!"
echo "    Run TagMan with:"
echo "      cd $INSTALL_DIR && ./tagman.sh"
echo ""
echo "    Tip: inside TagMan, Settings -> Folder Shortcut lets you drop a"
echo "    tagman.sh shortcut into any music folder so you can launch it"
echo "    straight from there next time."
