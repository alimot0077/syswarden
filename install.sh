#!/usr/bin/env bash
# SysWarden installer — per-user install, no root needed except for missing system packages.
#
#   curl -fsSL https://raw.githubusercontent.com/alimot0077/syswarden/main/install.sh | bash
#   or, from a clone:  ./install.sh
#
set -euo pipefail

REPO="alimot0077/syswarden"
BRANCH="${SYSWARDEN_BRANCH:-main}"
APP_ID="io.github.alimot0077.SysWarden"
EXT_UUID="syswarden@alimot0077.github.io"

DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
APP_DIR="$DATA_HOME/syswarden/app"
BIN_DIR="$HOME/.local/bin"
BIN="$BIN_DIR/syswarden"
EXT_DIR="$DATA_HOME/gnome-shell/extensions/$EXT_UUID"
UNIT_DIR="$CONFIG_HOME/systemd/user"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '  \033[34m•\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

bold "SysWarden installer"

[ "$(id -u)" -ne 0 ] || die "Run as your normal user, not root (SysWarden installs per user)."
command -v python3 >/dev/null || die "python3 is required."

# ---------------------------------------------------------------- source
SRC=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/app/main.py" ]; then
    SRC="$SCRIPT_DIR"
else
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT
    info "Downloading SysWarden ($BRANCH)…"
    # codeload is GitHub's archive host; it is reachable on some networks where github.com is not.
    curl -fsSL --connect-timeout 20 "https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH" -o "$TMP/src.tgz" \
        || curl -fsSL --connect-timeout 20 "https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz" -o "$TMP/src.tgz" \
        || die "Could not download SysWarden from GitHub. Check your connection (or proxy) and try again."
    tar -xzf "$TMP/src.tgz" -C "$TMP"
    rm -f "$TMP/src.tgz"
    SRC="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -n1)"
    [ -f "$SRC/app/main.py" ] || die "Download looks incomplete."
fi

# ---------------------------------------------------------------- dependencies
need=()
if command -v dpkg >/dev/null; then
    for pkg in python3-gi python3-psutil gir1.2-gtk-4.0 gir1.2-adw-1; do
        dpkg -s "$pkg" >/dev/null 2>&1 || need+=("$pkg")
    done
    if [ ${#need[@]} -gt 0 ]; then
        info "Installing missing packages: ${need[*]} (your password may be asked)"
        sudo apt-get update -qq
        sudo apt-get install -y "${need[@]}"
    fi
else
    python3 -c 'import gi, psutil; gi.require_version("Gtk","4.0"); gi.require_version("Adw","1")' 2>/dev/null \
        || die "Please install: PyGObject, psutil, GTK 4 and libadwaita for your distribution, then run again."
fi
python3 - <<'PY' || die "GTK 4 / libadwaita 1.5+ not found. Ubuntu 24.04 or newer is required."
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Adw
assert (Adw.get_major_version(), Adw.get_minor_version()) >= (1, 5)
PY

# ---------------------------------------------------------------- app
info "Installing the app to $APP_DIR"
systemctl --user stop syswarden-watch.service 2>/dev/null || true
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR" "$BIN_DIR"
cp -r "$SRC/app/." "$APP_DIR/"
find "$APP_DIR" -name '__pycache__' -prune -exec rm -rf {} +
cat > "$BIN" <<EOF
#!/bin/sh
exec /usr/bin/python3 "$APP_DIR/main.py" "\$@"
EOF
chmod +x "$BIN"

mkdir -p "$DATA_HOME/applications" "$DATA_HOME/icons/hicolor/scalable/apps"
sed "s|@BIN@|$BIN|g" "$SRC/data/$APP_ID.desktop.in" > "$DATA_HOME/applications/$APP_ID.desktop"
cp "$SRC/data/$APP_ID.svg" "$DATA_HOME/icons/hicolor/scalable/apps/$APP_ID.svg"
update-desktop-database "$DATA_HOME/applications" >/dev/null 2>&1 || true
gtk-update-icon-cache -q -t "$DATA_HOME/icons/hicolor" >/dev/null 2>&1 || true

# ---------------------------------------------------------------- background watcher
info "Enabling the background watcher (systemd user service)"
mkdir -p "$UNIT_DIR"
sed "s|@APPDIR@|$APP_DIR|g" "$SRC/data/syswarden-watch.service.in" > "$UNIT_DIR/syswarden-watch.service"
systemctl --user daemon-reload
systemctl --user enable --now syswarden-watch.service >/dev/null 2>&1 \
    || warn "Could not start the watcher; run: systemctl --user enable --now syswarden-watch"

# ---------------------------------------------------------------- GNOME Shell tile
NEED_RELOGIN=0
if command -v gnome-shell >/dev/null; then
    info "Installing the Quick Settings tile (GNOME Shell extension)"
    rm -rf "$EXT_DIR"
    mkdir -p "$EXT_DIR"
    cp -r "$SRC/extension/$EXT_UUID/." "$EXT_DIR/"
    if ! gnome-extensions enable "$EXT_UUID" >/dev/null 2>&1; then
        # The running shell has not seen the new extension yet: mark it enabled for the next login.
        current="$(gsettings get org.gnome.shell enabled-extensions 2>/dev/null || echo '@as []')"
        if [[ "$current" != *"$EXT_UUID"* ]]; then
            python3 - "$current" "$EXT_UUID" <<'PY' | xargs -0 gsettings set org.gnome.shell enabled-extensions
import ast, sys
cur, uuid = sys.argv[1], sys.argv[2]
items = [] if cur.strip() in ('@as []', '[]') else ast.literal_eval(cur.replace('@as ', ''))
items.append(uuid)
print(str(items), end='')
PY
        fi
        NEED_RELOGIN=1
    fi
else
    warn "GNOME Shell not found; skipping the Quick Settings tile (the app still works)."
fi

echo
bold "✓ SysWarden is installed"
echo "    Open it from the app grid (SysWarden) or run: syswarden"
if [ "$NEED_RELOGIN" = 1 ]; then
    echo "    Log out and back in once to see the SysWarden tile in Quick Settings (Wayland requires it)."
fi
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) warn "$BIN_DIR is not in your PATH; use the app grid or $BIN";; esac
echo "    Uninstall any time with: syswarden-uninstall  (or ./uninstall.sh)"
cp "$SRC/uninstall.sh" "$BIN_DIR/syswarden-uninstall" && chmod +x "$BIN_DIR/syswarden-uninstall"
