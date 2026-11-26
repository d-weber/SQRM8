"""Where things live on this OS, and the few OS services the app needs (macOS, Linux and Windows).

macOS:   logs in ~/Library/Logs; the headless daemon is a LaunchAgent (host/launchd/*.plist).
Linux:   logs in $XDG_STATE_HOME/sqrm8 (default ~/.local/state/...); the headless daemon is a systemd
         user service (host/systemd/sqrm8.service).
Windows: logs in %LOCALAPPDATA%\\sqrm8\\logs; no headless service (the tray app autostarts instead).
State (socket/port file, status.json, statusline.json, locks, settings.json) is ~/.sqrm8 everywhere
(%USERPROFILE%\\.sqrm8 on Windows), overridable with $SQRM8_HOME (tests). Before 0.8.0 the app was called
Claudius Maximus and kept it in ~/.claudius-maximus: migrate_state_dir() moves that over once.

This module must stay cheap to import (stdlib only, no subprocess at import time): the `hook` fast path and the
CLI import it.
"""
from __future__ import annotations

import json
import os
import sys
import threading

IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
IS_WINDOWS = sys.platform == "win32"

SERVICE_LABEL = "com.sqrm8.daemon"      # launchd label (headless daemon, tools/install.sh)
SERVICE_UNIT = "sqrm8.service"          # systemd user unit (headless daemon, tools/install.sh)
APP_NAME = "SQRM8"
# The same things under the app's name before 0.8.0 (Claudius Maximus): recognised so an upgrade can retire them.
LEGACY_STATE_DIR = ".claudius-maximus"
LEGACY_SERVICE_LABEL = "com.claudius-maximus.daemon"
LEGACY_SERVICE_UNIT = "claudius-maximus.service"


# ---------------------------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------------------------
def home() -> str:
    return os.path.expanduser("~")


def env(name: str, default: str | None = None) -> str | None:
    """$SQRM8_<name>, else the pre-0.8.0 $CLAUDIUS_<name> (existing dev setups keep working), else default."""
    return os.environ.get("SQRM8_" + name) or os.environ.get("CLAUDIUS_" + name) or default


def state_dir() -> str:
    """~/.sqrm8 (socket / port file, status.json, statusline.json, locks, settings.json)."""
    return env("HOME") or os.path.join(home(), ".sqrm8")


def migrate_state_dir() -> str | None:
    """Move ~/.claudius-maximus (pre-0.8.0 name) to ~/.sqrm8 when only the old one exists, so an upgrade keeps
    the settings, the hooks manifest and the chained statusLine. Returns one line saying what happened (for the
    caller's log), None when there was nothing to do. Never deletes the old directory except as the second half
    of a completed copy (rename across filesystems); never touches anything when $SQRM8_HOME (or the old
    $CLAUDIUS_HOME) is set. Cheap when there is nothing to do (two stats), so every entry point calls it."""
    if env("HOME"):
        return None
    new, old = os.path.join(home(), ".sqrm8"), os.path.join(home(), LEGACY_STATE_DIR)
    if os.path.lexists(new) or not os.path.isdir(old):
        return None
    import errno
    try:
        os.rename(old, new)
    except OSError as e:
        if e.errno != errno.EXDEV:   # e.g. another process won the race, or a file is held open (Windows)
            return None if os.path.lexists(new) else f"could not move {old} to {new}: {e}"
        import shutil
        try:
            shutil.copytree(old, new, symlinks=True)
        except OSError as e2:
            shutil.rmtree(new, ignore_errors=True)   # a partial copy would stop the next start from retrying
            return f"could not copy {old} to {new}: {e2}"
        shutil.rmtree(old, ignore_errors=True)
    return f"moved the state directory {old} to {new} (the app is now called {APP_NAME})"


def log_dir() -> str:
    if IS_MAC:
        return os.path.join(home(), "Library", "Logs")
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.join(home(), "AppData", "Local")
        return os.path.join(base, "sqrm8", "logs")
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(home(), ".local", "state")
    return os.path.join(base, "sqrm8")


def log_path() -> str:
    return os.path.join(log_dir(), "sqrm8.log")


def claude_config_dir() -> str:
    """Claude Code's config dir ($CLAUDE_CONFIG_DIR or ~/.claude; %USERPROFILE%\\.claude on Windows)."""
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(home(), ".claude")


claude_dir = claude_config_dir


def gemini_config_dir() -> str:
    """Antigravity global customization dir ($GEMINI_CONFIG_DIR or ~/.gemini/config), where hooks.json lives."""
    return os.environ.get("GEMINI_CONFIG_DIR") or os.path.join(home(), ".gemini", "config")


def antigravity_dirs() -> list[tuple[str, str]]:
    """(surface, dir) pairs for Antigravity app data, CLI first (agy), then Antigravity 2.0 App and IDE."""
    base = os.path.join(home(), ".gemini")
    return [
        ("cli", os.environ.get("ANTIGRAVITY_CLI_DIR") or os.path.join(base, "antigravity-cli")),
        ("app", os.environ.get("ANTIGRAVITY_APP_DIR") or os.path.join(base, "antigravity")),
        ("ide", os.environ.get("ANTIGRAVITY_IDE_DIR") or os.path.join(base, "antigravity-ide")),
    ]


def opencode_data_dir() -> str:
    """OpenCode data dir ($OPENCODE_DATA, $XDG_DATA_HOME/opencode, %LOCALAPPDATA%\\opencode, or ~/.local/share/opencode)."""
    if os.environ.get("OPENCODE_DATA"):
        return os.environ["OPENCODE_DATA"]
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.join(home(), "AppData", "Local")
        return os.path.join(base, "opencode")
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(home(), ".local", "share")
    return os.path.join(base, "opencode")


def enabled_agents() -> set[str] | None:
    """Optional `agents` list in settings.json; None means auto (run all available agent providers simultaneously)."""
    val = settings_get("agents")
    if isinstance(val, list):
        return {str(x).strip().lower() for x in val if str(x).strip()}
    return None


def settings_path() -> str:
    """The app's own settings (board choice, auto flash, first_run_done...), not Claude Code's."""
    return os.path.join(state_dir(), "settings.json")


def service_path(legacy: bool = False) -> str | None:
    """The headless background-service definition (LaunchAgent plist / systemd user unit); legacy=True: the
    one tools/install.sh wrote before 0.8.0 under the old name. None on Windows, which has no headless service."""
    if IS_MAC:
        label = LEGACY_SERVICE_LABEL if legacy else SERVICE_LABEL
        return os.path.join(home(), "Library", "LaunchAgents", f"{label}.plist")
    if IS_WINDOWS:
        return None
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home(), ".config")
    return os.path.join(base, "systemd", "user", LEGACY_SERVICE_UNIT if legacy else SERVICE_UNIT)


def service_name() -> str:
    return "launch agent" if IS_MAC else "service" if IS_WINDOWS else "systemd unit"


def installed_service_path() -> str | None:
    """The headless service definition that is on disk: the current name first, else the pre-0.8.0 one."""
    for legacy in (False, True):
        p = service_path(legacy)
        if p and os.path.exists(p):
            return p
    return None


def service_installed() -> bool:
    return installed_service_path() is not None


# ---------------------------------------------------------------------------------------------
# running ourselves
# ---------------------------------------------------------------------------------------------
def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def package_root() -> str:
    """Directory that contains the `companion` package (host/ in the repo)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def self_command() -> list[str]:
    """argv prefix that runs this program: [sys.executable] when frozen, else [python, "-m", "companion"].
    An AppImage runs from a temporary mount point that changes every launch, so use $APPIMAGE (the .AppImage
    file itself) when set. The non-frozen form needs package_root() as cwd (or companion installed); see
    self_cwd()."""
    if is_frozen():
        return [os.environ.get("APPIMAGE") or sys.executable]
    return [sys.executable, "-m", "companion"]


def self_cwd() -> str | None:
    """Working directory self_command() needs (None when frozen: the binary finds its own modules)."""
    return None if is_frozen() else package_root()


def pid_alive(pid) -> bool:
    """Is a process with this pid running? (os.kill(pid, 0) would *terminate* it on Windows.)"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if IS_WINDOWS:
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return ctypes.GetLastError() == 5  # access denied: exists, owned by someone else
            try:
                code = ctypes.c_ulong()
                if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return True
                return code.value == 259  # STILL_ACTIVE
            finally:
                k32.CloseHandle(h)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def open_path(path: str) -> bool:
    """Open a file / folder / URL with the desktop's default handler. Best effort, never blocks."""
    import subprocess
    try:
        if IS_WINDOWS:
            os.startfile(path)  # type: ignore[attr-defined]
            return True
        cmd = ["open", path] if IS_MAC else ["xdg-open", path]
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return True
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------------------------
# app settings: tiny JSON key/value store at settings_path()
# ---------------------------------------------------------------------------------------------
class Settings:
    """Read on every get (the CLI and the app may both write), written atomically (tmp + replace)."""

    def __init__(self, path: str | None = None):
        self._path = path
        self._lock = threading.Lock()

    @property
    def path(self) -> str:
        return self._path or settings_path()

    def all(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, key: str, default=None):
        return self.all().get(key, default)

    def set(self, key: str, value) -> None:
        with self._lock:
            d = self.all()
            if value is None:
                d.pop(key, None)
            else:
                d[key] = value
            self._write(d)

    def _write(self, d: dict) -> None:
        import tempfile
        path = self.path
        folder = os.path.dirname(path)
        os.makedirs(folder, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".settings.", suffix=".tmp", dir=folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=1, sort_keys=True)
                f.write("\n")
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


settings = Settings()


def settings_get(key: str, default=None):
    return settings.get(key, default)


def settings_set(key: str, value) -> None:
    """Set a key (None removes it). Raises OSError if the state dir is not writable."""
    settings.set(key, value)
