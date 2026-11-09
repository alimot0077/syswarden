#!/usr/bin/env bash
# Removes everything SysWarden installed for this user. Settings are kept unless you pass --purge.
set -uo pipefail

APP_ID="io.github.alimot0077.SysWarden"
EXT_UUID="syswarden@alimot0077.github.io"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"

echo "Uninstalling SysWarden…"
systemctl --user disable --now syswarden-watch.service >/dev/null 2>&1
rm -f "$CONFIG_HOME/systemd/user/syswarden-watch.service"
systemctl --user daemon-reload >/dev/null 2>&1
gnome-extensions disable "$EXT_UUID" >/dev/null 2>&1
rm -rf "$DATA_HOME/gnome-shell/extensions/$EXT_UUID"
rm -rf "$DATA_HOME/syswarden"
rm -f "$DATA_HOME/applications/$APP_ID.desktop" "$DATA_HOME/icons/hicolor/scalable/apps/$APP_ID.svg"
rm -f "$HOME/.local/bin/syswarden"
if [ "${1:-}" = "--purge" ]; then
    rm -rf "$CONFIG_HOME/syswarden"
fi
rm -f "$HOME/.local/bin/syswarden-uninstall"
echo "✓ SysWarden removed."
