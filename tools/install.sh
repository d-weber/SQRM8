#!/bin/bash
# SQRM8 — headless install of the host side from a source checkout (macOS or Linux).
#   tools/install.sh [--no-service] [--no-hooks] [--no-statusline]      (--no-launchd = --no-service)
# Idempotent: safe to re-run (e.g. after moving the repo or updating).
#  1. Python venv in host/.venv with pyserial (+ psutil, optional)
#  2. Claude Code hooks + statusLine wrapper merged into ~/.claude/settings.json (timestamped backup)
#  3. background service: macOS LaunchAgent ~/Library/LaunchAgents/com.sqrm8.daemon.plist
#     (RunAtLoad, KeepAlive) or Linux systemd user unit ~/.config/systemd/user/sqrm8.service; the service of a
#     "Claudius Maximus" checkout (the name before 0.8.0) is stopped and removed first
# The normal path is the desktop app from the GitHub Releases page (tray icon, flasher, start at login); this
# script is for servers and development. The app and this service cannot both own the device: with both, the
# app only shows the service's status; the app's "Start at login" stops and disables the service.
# If $HOME is not your real home directory (testing), launchctl/systemctl are never called.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL=com.sqrm8.daemon
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/sqrm8.service"
STATE="$HOME/.sqrm8"
# what the same service was called before the rename to SQRM8 (0.8.0)
OLD_LABEL=com.claudius-maximus.daemon
OLD_PLIST="$HOME/Library/LaunchAgents/$OLD_LABEL.plist"
OLD_UNIT="$UNIT_DIR/claudius-maximus.service"
OLD_STATE="$HOME/.claudius-maximus"
OLD_UDEV=/etc/udev/rules.d/99-claudius-maximus.rules
VENV="$ROOT/host/.venv"
PY="$VENV/bin/python"
OS="$(uname -s)"
if [ "$OS" = Darwin ]; then
  LOG="$HOME/Library/Logs/sqrm8.log"
else
  LOG="${XDG_STATE_HOME:-$HOME/.local/state}/sqrm8/sqrm8.log"
fi
DO_LAUNCHD=1; DO_HOOKS=1; SL_FLAG=""
for a in "$@"; do
  case "$a" in
    --no-launchd|--no-service) DO_LAUNCHD=0 ;;
    --no-hooks) DO_HOOKS=0 ;;
    --no-statusline) SL_FLAG="--no-statusline" ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown option $a" >&2; exit 2 ;;
  esac
done

REAL_HOME="$(python3 -c 'import pwd,os;print(pwd.getpwuid(os.getuid()).pw_dir)' 2>/dev/null || echo "$HOME")"
if [ "$HOME" != "$REAL_HOME" ]; then
  echo "note: HOME=$HOME is not the real home ($REAL_HOME): test mode, the service will not be started"
  TEST_MODE=1
else
  TEST_MODE=0
fi

echo "==> Python environment ($VENV)"
if [ ! -x "$PY" ]; then
  BASEPY="$(command -v python3.15 || command -v python3.14 || command -v python3.13 || command -v python3.12 || command -v python3.11 || command -v python3)"
  "$BASEPY" -m venv "$VENV"
fi
if ! "$PY" -c 'import serial' 2>/dev/null; then
  "$PY" -m pip install --quiet --disable-pip-version-check pyserial
fi
# optional: host metrics for the System screen (providers/system.py degrades gracefully without it)
"$PY" -c 'import psutil' 2>/dev/null || "$PY" -m pip install --quiet --disable-pip-version-check psutil \
  || echo "  (psutil not installed: System screen metrics disabled)"
"$PY" -c 'import serial, sys; print("  python", sys.version.split()[0], "pyserial", serial.VERSION)'

mkdir -p "$(dirname "$LOG")"
# ~/.claudius-maximus (the name before 0.8.0) holds the port choice, the hooks manifest and the chained
# statusLine: let the Python side move it to ~/.sqrm8 now, before the hooks step writes there. If that fails,
# an empty ~/.sqrm8 made here would stop the daemon from retrying the move, so leave both alone then.
PYTHONPATH="$ROOT/host" "$PY" -c 'from companion import hostos; m = hostos.migrate_state_dir(); m and print("  " + m)' \
  || echo "  (could not check for $OLD_STATE)"
if [ -d "$OLD_STATE" ] && [ ! -e "$STATE" ]; then
  echo "  $OLD_STATE is still there: the daemon moves it to $STATE on its first start"
else
  mkdir -p "$STATE"
  chmod 700 "$STATE"
fi

if [ "$OS" = Linux ] && [ -e /dev/ttyACM0 ] 2>/dev/null; then
  if ! id -nG | tr ' ' '\n' | grep -qx dialout && ! [ -w /dev/ttyACM0 ]; then
    echo "  warning: you cannot write to the serial port. Either: sudo usermod -aG dialout $USER (then log out/in)"
    echo "           or install the udev rule: sudo cp $ROOT/tools/udev/99-sqrm8.rules /etc/udev/rules.d/"
  fi
fi
# Not removed here (needs root); the two rules are identical, so the old one only needs renaming.
if [ "$OS" = Linux ] && [ -e "$OLD_UDEV" ]; then
  echo "  note: $OLD_UDEV is the rule's old name; replace it with:"
  echo "        sudo rm $OLD_UDEV && sudo cp $ROOT/tools/udev/99-sqrm8.rules /etc/udev/rules.d/"
fi

if [ "$DO_HOOKS" = 1 ]; then
  echo "==> Agent hooks (Claude Code / Antigravity)"
  "$PY" "$ROOT/host/hooks/sqrm8_settings.py" install --agent auto --root "$ROOT" --python "$PY" $SL_FLAG
fi

if [ "$DO_LAUNCHD" = 1 ] && [ "$OS" = Darwin ]; then
  if [ -e "$OLD_PLIST" ] || [ -e "$OLD_PLIST.disabled-by-app" ]; then
    echo "==> Removing the old LaunchAgent ($OLD_LABEL)"
    if [ "$TEST_MODE" = 0 ]; then
      launchctl bootout "gui/$(id -u)/$OLD_LABEL" 2>/dev/null || true
    fi
    rm -f "$OLD_PLIST" "$OLD_PLIST.disabled-by-app"
  fi
  echo "==> LaunchAgent ($PLIST)"
  mkdir -p "$HOME/Library/LaunchAgents"
  sed -e "s|@PYTHON@|$PY|g" -e "s|@ROOT@|$ROOT|g" -e "s|@HOME@|$HOME|g" \
      "$ROOT/host/launchd/$LABEL.plist" > "$PLIST.tmp"
  /usr/bin/plutil -lint "$PLIST.tmp" >/dev/null
  mv "$PLIST.tmp" "$PLIST"
  if [ "$TEST_MODE" = 0 ]; then
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    launchctl enable "gui/$(id -u)/$LABEL" || true
    echo "  daemon started"
  else
    echo "  (test mode: skipped launchctl bootstrap)"
  fi
elif [ "$DO_LAUNCHD" = 1 ]; then
  HAVE_SYSTEMD=0
  if [ "$TEST_MODE" = 0 ] && command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
    HAVE_SYSTEMD=1
  fi
  if [ -e "$OLD_UNIT" ]; then
    echo "==> Removing the old systemd user unit ($OLD_UNIT)"
    if [ "$HAVE_SYSTEMD" = 1 ]; then
      systemctl --user disable --now claudius-maximus.service 2>/dev/null || true
    fi
    rm -f "$OLD_UNIT"
  fi
  echo "==> systemd user unit ($UNIT)"
  mkdir -p "$UNIT_DIR"
  sed -e "s|@PYTHON@|$PY|g" -e "s|@ROOT@|$ROOT|g" -e "s|@LOG@|$LOG|g" \
      "$ROOT/host/systemd/sqrm8.service" > "$UNIT.tmp"
  mv "$UNIT.tmp" "$UNIT"
  if [ "$HAVE_SYSTEMD" = 1 ]; then
    systemctl --user daemon-reload
    systemctl --user enable sqrm8.service >/dev/null
    systemctl --user restart sqrm8.service
    echo "  daemon started"
  else
    echo "  (skipped systemctl: test mode or no user systemd; start manually: cd $ROOT/host && $PY -m companion)"
  fi
fi

echo
echo "Done. Check with:   $PY -m companion status      (run from $ROOT/host)"
echo "Logs:               $LOG"
echo "Hooks take effect in new Claude Code sessions (or after /hooks reload)."
echo "Desktop app instead (tray, firmware updates): https://github.com/d-weber/SQRM8/releases"
echo "  It cannot share the device with this service; enabling its Start at login disables the service."
echo "Remove with:        $ROOT/tools/uninstall.sh   (also removes the app's start-at-login entry, if any)"
