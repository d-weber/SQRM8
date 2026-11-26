#!/bin/bash
# SQRM8 — reverse tools/install.sh exactly (macOS or Linux):
#  stop + remove the background service, remove our hooks / statusLine from ~/.claude/settings.json (restoring
#  a chained statusLine; timestamped backup first), delete ~/.sqrm8 and our log files.
# Also removes the desktop app's start-at-login entry if present (best effort): ~/.config/autostart/
#  sqrm8.desktop (Linux), ~/Library/LaunchAgents/com.sqrm8.app.plist (macOS), and the service plist the app set
#  aside (*.plist.disabled-by-app). It does not quit a running app or delete the app itself (AppImage,
#  /Applications/SQRM8.app); quit it from its menu first, or it recreates ~/.sqrm8. The desktop app alone: see
#  README "Uninstall".
# Everything installed under the name before 0.8.0 ("Claudius Maximus": com.claudius-maximus.*,
#  claudius-maximus.service/.desktop, ~/.claudius-maximus, its logs) is removed the same way.
# The repo and its host/.venv are left in place.   tools/uninstall.sh [--keep-logs]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LA="$HOME/Library/LaunchAgents"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
AUTOSTART_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
STATE_HOME="${XDG_STATE_HOME:-${HOME:?}/.local/state}"
# current name first, then the name before the rename to SQRM8 (0.8.0)
NAMES="sqrm8 claudius-maximus"
OS="$(uname -s)"
KEEP_LOGS=0
[ "${1:-}" = "--keep-logs" ] && KEEP_LOGS=1

REAL_HOME="$(python3 -c 'import pwd,os;print(pwd.getpwuid(os.getuid()).pw_dir)' 2>/dev/null || echo "$HOME")"

if [ "$OS" = Darwin ]; then
  for n in $NAMES; do
    LABEL="com.$n.daemon"
    PLIST="$LA/$LABEL.plist"
    [ "$n" = sqrm8 ] || [ -e "$PLIST" ] || [ -e "$PLIST.disabled-by-app" ] || continue
    echo "==> LaunchAgent $LABEL"
    if [ "$HOME" = "$REAL_HOME" ]; then
      launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null && echo "  stopped" || echo "  not loaded"
    else
      echo "  (test mode: HOME=$HOME, skipped launchctl)"
    fi
    rm -f "$PLIST" "$PLIST.disabled-by-app"
  done
else
  for n in $NAMES; do
    UNIT="$UNIT_DIR/$n.service"
    [ "$n" = sqrm8 ] || [ -e "$UNIT" ] || continue
    echo "==> systemd user unit $n.service"
    if [ "$HOME" = "$REAL_HOME" ] && command -v systemctl >/dev/null; then
      systemctl --user disable --now "$n.service" 2>/dev/null && echo "  stopped" || echo "  not loaded"
    else
      echo "  (test mode: HOME=$HOME, skipped systemctl)"
    fi
    rm -f "$UNIT"
  done
  command -v systemctl >/dev/null && systemctl --user daemon-reload 2>/dev/null || true
  # Not removed here (needs root): just say how.
  for n in $NAMES; do
    RULE="/etc/udev/rules.d/99-$n.rules"
    if [ -e "$RULE" ]; then echo "  udev rule left in place (needs root): sudo rm $RULE"; fi
  done
fi

echo "==> Desktop app start-at-login entry"
APP_FOUND=0
for n in $NAMES; do
  for f in "$LA/com.$n.app.plist" "$AUTOSTART_DIR/$n.desktop"; do
    if [ -e "$f" ]; then
      rm -f "$f" && echo "  removed $f" || echo "  could not remove $f"
      APP_FOUND=1
    fi
  done
done
[ "$APP_FOUND" = 1 ] || echo "  none"
# (not booted out: launchd only reads the app's plist at login, and booting it out would kill a running app)

echo "==> Agent hooks (Claude Code / Antigravity)"
PY="$ROOT/host/.venv/bin/python"
[ -x "$PY" ] || PY=/usr/bin/python3
# a checkout last installed as "Claudius Maximus": move its state first, so the uninstall finds the hooks
# manifest and restores the chained statusLine from it
PYTHONPATH="$ROOT/host" "$PY" -c 'from companion import hostos; hostos.migrate_state_dir()' 2>/dev/null || true
"$PY" "$ROOT/host/hooks/sqrm8_settings.py" uninstall --agent all

echo "==> State and logs"
for n in $NAMES; do
  rm -rf "${HOME:?}/.$n"
  if [ "$KEEP_LOGS" = 0 ]; then
    rm -f "${HOME:?}/Library/Logs/$n.log"* "${HOME:?}/Library/Logs/$n.out.log"
    rm -rf "${STATE_HOME:?}/$n"
  fi
done
echo "Done. (settings backups: ~/.claude/settings.json.bak-*)"
echo "If you also use the desktop app, quit it from its menu and delete it (AppImage / SQRM8.app);"
echo "on its next start it would install the hooks and start at login again."
