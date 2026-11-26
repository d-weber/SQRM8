"""Idempotently add / remove the SQRM8 hooks + statusLine wrapper in ~/.claude/settings.json.

    python host/hooks/sqrm8_settings.py install   --root <project root> --python <venv python> [--no-statusline]
    python host/hooks/sqrm8_settings.py uninstall
    python host/hooks/sqrm8_settings.py check      (exit 0 if installed, 3 if under the pre-0.8.0 names)
    sqrm8 hooks install|uninstall|check [--no-statusline]   (install_for_app: right command for
                                                              the running program)

Lives in the companion package (host/hooks/sqrm8_settings.py is a shim) so a frozen desktop app has it too.

Command forms written (double quotes: they work in bash, zsh and cmd; Claude Code on Windows runs hooks in
Git Bash or cmd):
    script install   "<python>" -I -S "<root>/host/hooks/sqrm8_hook.py"   /   ... sqrm8_statusline.py
    frozen app       "<exe>" hook                                          /   "<exe>" statusline
    pip install      "<python>" -m companion hook                          /   ... statusline
Ours are recognised in all three forms (is_ours_command), so switching between the script install and the
app replaces the entries instead of duplicating them. So are the forms written before 0.8.0, when the app was
called Claudius Maximus (cm_hook.py / cm_statusline.py, a claudius-maximus / ClaudiusMaximus exe): install
replaces them like any entry of ours, and check() reports them as needing a reinstall (is_legacy_command).

* Makes a timestamped backup (settings.json.bak-sqrm8-YYYYmmdd-HHMMSS) before any change.
* Never touches hook entries it didn't create: ours are recognised by is_ours_command() (see above).
  Re-running install replaces our entries (e.g. after moving the repo) and leaves everything else alone.
* statusLine: if the user already has one, it is saved to ~/.sqrm8/statusline-chain.json and
  our wrapper runs it and prints its output (so the status line looks the same). Uninstall restores it.
* A manifest (~/.sqrm8/install-manifest.json) remembers what existed before, so uninstall
  reverses exactly (e.g. removes an empty "hooks" key only if install created it).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sys
import time

HOOK_MARK = "sqrm8_hook.py"
SL_MARK = "sqrm8_statusline.py"
LEGACY_MARKS = {"hook": "cm_hook.py", "statusline": "cm_statusline.py"}   # the shims' names before 0.8.0
# `"<exe>" hook [--agent ...]` where the exe is our app (sqrm8 / SQRM8.exe / SQRM8-0.8.0-linux-x64.AppImage...), or
# `... -m companion hook [--agent ...]`. Matched on the whole command, case-insensitive.
_APP_RE = {
    "hook": re.compile(r'(?i)(sqrm8[^"\s]*"?|-m\s+companion)\s+hook(\s+--agent[=\s][a-z0-9_-]+)?\s*$'),
    "statusline": re.compile(r'(?i)(sqrm8[^"\s]*"?|-m\s+companion)\s+statusline\s*$'),
}
# The same for the app before 0.8.0 (claudius-maximus / ClaudiusMaximus / Claudius Maximus.app...).
_LEGACY_APP_RE = {
    "hook": re.compile(r'(?i)claudius[ _-]?maximus[^"\s]*"?\s+hook\s*$'),
    "statusline": re.compile(r'(?i)claudius[ _-]?maximus[^"\s]*"?\s+statusline\s*$'),
}

# Antigravity global hooks (`~/.gemini/config/hooks.json`).
# PreToolUse is intentionally OMITTED: Antigravity treats PreToolUse as a fail-closed permission gate
# (`{}` denies tools; `"allow"` would bypass user permission prompts). Pre-execution tool activity and
# `ask_question` prompts come from Antigravity's live transcript.jsonl instead.
ANTIGRAVITY_HOOK_KEY = "sqrm8"
ANTIGRAVITY_FLAT_EVENTS = ("SessionStart", "PreInvocation", "PostInvocation", "Stop")
ANTIGRAVITY_GROUPED_EVENTS = ("PostToolUse",)

# event -> matcher (None = all)
EVENTS = {
    "SessionStart": None,
    "SessionEnd": None,
    "UserPromptSubmit": None,
    "PreToolUse": None,
    "PostToolUse": None,
    "PostToolUseFailure": None,
    "PermissionRequest": None,
    "PermissionDenied": None,
    "Notification": "permission_prompt|idle_prompt|elicitation_dialog",
    "Stop": None,
    "StopFailure": None,
    "SubagentStart": None,
    "SubagentStop": None,
    "PreCompact": None,
}
# Run synchronously (our hook takes ~30 ms and prints nothing): end-of-turn/session events, where an async
# hook can be lost if the claude process exits right after (seen with `claude -p`). Everything else is async.
SYNC_EVENTS = {"Stop", "StopFailure", "SessionEnd", "SubagentStop"}


def home() -> str:
    return os.path.expanduser("~")


def state_dir() -> str:
    return os.environ.get("SQRM8_HOME") or os.environ.get("CLAUDIUS_HOME") or os.path.join(home(), ".sqrm8")


def settings_path() -> str:
    """Claude Code's user settings ($CLAUDE_CONFIG_DIR or ~/.claude)."""
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(home(), ".claude")
    return os.path.join(base, "settings.json")


def antigravity_hooks_path() -> str:
    """Antigravity's global hooks file ($GEMINI_CONFIG_DIR/hooks.json or ~/.gemini/config/hooks.json)."""
    base = os.environ.get("GEMINI_CONFIG_DIR") or os.path.join(home(), ".gemini", "config")
    return os.path.join(base, "hooks.json")


def claude_detected() -> bool:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(home(), ".claude")
    return os.path.isdir(base) or bool(shutil.which("claude"))


def antigravity_detected() -> bool:
    """One of Antigravity's own data directories, or `agy` on the PATH. ~/.gemini alone is not enough: it is
    the Gemini CLI's directory too, and "detected" is what lets an install write a hooks.json there."""
    from .hostos import antigravity_dirs
    if any(os.path.isdir(d) for _, d in antigravity_dirs()):
        return True
    return bool(shutil.which("agy"))


def detected_agents() -> list[str]:
    found = []
    if claude_detected():
        found.append("claude")
    if antigravity_detected():
        found.append("antigravity")
    return found


# ---------------------------------------------------------------------------------------------
# command forms
# ---------------------------------------------------------------------------------------------
def quote(arg: str) -> str:
    """Double-quote for both POSIX shells and cmd. Inside POSIX double quotes $ ` \\ " are still special:
    fall back to single quotes there if the path has any (cmd can't run such paths anyway)."""
    if os.name != "nt" and any(c in arg for c in '$`"\\'):
        return shlex.quote(arg)
    return f'"{arg}"'


def script_command(python: str, script: str, agent: str = "claude") -> str:
    cmd = f"{quote(python)} -I -S {quote(script)}"
    if agent and agent != "claude":
        cmd += f" --agent {agent}"
    return cmd


def hooks_script_dir() -> str | None:
    """host/hooks in a repo checkout (where sqrm8_hook.py lives next to the companion package), else None."""
    d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks")
    return d if os.path.exists(os.path.join(d, HOOK_MARK)) else None


def hook_command(which: str = "hook", agent: str = "claude") -> str:
    """The command that runs our hook (`which`="hook") or statusLine wrapper ("statusline") for the running
    program: frozen app -> "<exe>" hook; repo checkout -> "<python>" -I -S "<host/hooks/sqrm8_hook.py>";
    otherwise (pip install) -> "<python>" -m companion hook."""
    suffix = f" --agent {agent}" if (which == "hook" and agent and agent != "claude") else ""
    if getattr(sys, "frozen", False):
        exe = os.environ.get("APPIMAGE") or sys.executable
        return f"{quote(exe)} {which}{suffix}"
    d = hooks_script_dir()
    if d:
        return script_command(sys.executable, os.path.join(d, HOOK_MARK if which == "hook" else SL_MARK),
                              agent=agent if which == "hook" else "claude")
    return f"{quote(sys.executable)} -m companion {which}{suffix}"


def _is_current_command(cmd: str, which: str) -> bool:
    if (HOOK_MARK if which == "hook" else SL_MARK) in cmd:
        return True
    return bool(_APP_RE[which].search(cmd.strip()))


def is_ours_command(cmd, which: str = "hook") -> bool:
    """Is this hook / statusLine command one of ours (any of the three forms, current or pre-0.8.0 names)?"""
    cmd = str(cmd or "")
    return _is_current_command(cmd, which) or is_legacy_command(cmd, which)


def is_legacy_command(cmd, which: str = "hook") -> bool:
    """One of ours written before the rename to SQRM8 (0.8.0): it runs a shim or an exe under the old name,
    which an upgrade removes or renames, so it needs a reinstall. A current form wins: the macOS updater of
    0.7.x copies the new bundle over "Claudius Maximus.app", whose exe is then .../MacOS/sqrm8."""
    cmd = str(cmd or "")
    if _is_current_command(cmd, which):
        return False
    return LEGACY_MARKS[which] in cmd or bool(_LEGACY_APP_RE[which].search(cmd.strip()))


def load_settings(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    # utf-8-sig: Claude Code writes UTF-8 (non-ASCII paths / user names); an editor may have added a BOM. The
    # locale encoding (cp1252 on Windows) would mangle or reject such a file.
    with open(path, encoding="utf-8-sig") as f:
        txt = f.read()
    if not txt.strip():
        return {}
    d = json.loads(txt)  # raise on invalid JSON: never clobber a file we can't parse
    if not isinstance(d, dict):
        raise ValueError("settings.json is not a JSON object")
    return d


def save_settings(path: str, d: dict, backup: bool = True):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if backup and os.path.exists(path):
        b = path + time.strftime(".bak-sqrm8-%Y%m%d-%H%M%S")
        with open(path, "rb") as src, open(b, "wb") as dst:
            dst.write(src.read())
        print(f"  backup: {b}")
    mode = os.stat(path).st_mode & 0o777 if os.path.exists(path) else 0o644
    tmp = path + ".sqrm8.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _is_ours(handler, mark=HOOK_MARK) -> bool:
    which = "statusline" if mark == SL_MARK else "hook"
    return isinstance(handler, dict) and is_ours_command(handler.get("command", ""), which)


def strip_hooks(d: dict) -> bool:
    """Remove our hook handlers. Returns True if anything changed."""
    hooks = d.get("hooks")
    if not isinstance(hooks, dict):
        return False
    changed = False
    for ev in list(hooks.keys()):
        groups = hooks[ev]
        if not isinstance(groups, list):
            continue
        new_groups = []
        for g in groups:
            if isinstance(g, dict) and isinstance(g.get("hooks"), list):
                kept = [h for h in g["hooks"] if not _is_ours(h, HOOK_MARK)]
                if len(kept) != len(g["hooks"]):
                    changed = True
                    if not kept:
                        continue  # group only contained our handler
                    g = dict(g, hooks=kept)
            new_groups.append(g)
        if new_groups:
            hooks[ev] = new_groups
        else:
            del hooks[ev]
            changed = True
    return changed


def read_manifest() -> dict:
    try:
        with open(os.path.join(state_dir(), "install-manifest.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_manifest(m: dict):
    os.makedirs(state_dir(), mode=0o700, exist_ok=True)
    with open(os.path.join(state_dir(), "install-manifest.json"), "w", encoding="utf-8") as f:
        json.dump(m, f, indent=1)


def install(root: str, python: str, statusline: bool) -> int:
    """Script install (tools/install.sh): hooks run <root>/host/hooks/cm_*.py with `python -I -S`."""
    return install_commands(script_command(python, os.path.join(root, "host", "hooks", HOOK_MARK)),
                            script_command(python, os.path.join(root, "host", "hooks", SL_MARK)),
                            statusline, root=root)


def install_for_app(statusline: bool = True) -> int:
    """Install with the right command for the running program (see hook_command)."""
    root = os.path.dirname(os.path.dirname(hooks_script_dir())) if hooks_script_dir() and not getattr(
        sys, "frozen", False) else ""
    return install_commands(hook_command("hook"), hook_command("statusline"), statusline, root=root)


def install_commands(hook_cmd: str, sl_cmd: str, statusline: bool, root: str = "") -> int:
    path = settings_path()
    existed = os.path.exists(path)
    d = load_settings(path)
    m = read_manifest()
    if "had_hooks_key" not in m:  # first install: remember the original shape
        m["had_settings_file"] = existed
        m["had_hooks_key"] = "hooks" in d
        m["had_statusline"] = "statusLine" in d

    before = json.dumps(d, sort_keys=True)
    strip_hooks(d)
    hooks = d.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        print("error: settings.json 'hooks' is not an object; aborting", file=sys.stderr)
        return 2
    for ev, matcher in EVENTS.items():
        h = {"type": "command", "command": hook_cmd, "timeout": 5}
        if ev not in SYNC_EVENTS:
            h["async"] = True
        group = {"hooks": [h]}
        if matcher:
            group = {"matcher": matcher, **group}
        lst = hooks.setdefault(ev, [])
        if not isinstance(lst, list):
            print(f"warning: hooks.{ev} is not a list; skipping", file=sys.stderr)
            continue
        lst.append(group)

    if statusline:
        sl = d.get("statusLine")
        if isinstance(sl, dict) and is_ours_command(sl.get("command", ""), "statusline"):
            sl["command"] = sl_cmd  # already ours: refresh path / switch form
        else:
            if isinstance(sl, dict) and sl.get("command"):
                os.makedirs(state_dir(), mode=0o700, exist_ok=True)
                with open(os.path.join(state_dir(), "statusline-chain.json"), "w", encoding="utf-8") as f:
                    json.dump({"command": sl["command"]}, f)
                m["statusline_prev"] = sl
                print(f"  statusLine: chaining your existing command: {sl['command']}")
            new = dict(sl) if isinstance(sl, dict) else {"type": "command", "padding": 0}
            new["type"] = "command"
            new["command"] = sl_cmd
            d["statusLine"] = new
        m["statusline_installed"] = True

    m["installed_at"] = int(time.time())
    m["root"] = root
    m["hook_command"] = hook_cmd
    if json.dumps(d, sort_keys=True) != before:
        save_settings(path, d)
        print(f"  updated {path}")
    else:
        print(f"  {path} already up to date")
    write_manifest(m)
    return 0


def uninstall() -> int:
    path = settings_path()
    try:
        d = load_settings(path)
    except (ValueError, OSError) as e:
        print(f"error: cannot parse {path}: {e}; not touching it", file=sys.stderr)
        return 2
    m = read_manifest()
    before = json.dumps(d, sort_keys=True)
    strip_hooks(d)
    if d.get("hooks") == {} and not m.get("had_hooks_key", True):
        del d["hooks"]
    sl = d.get("statusLine")
    if isinstance(sl, dict) and is_ours_command(sl.get("command", ""), "statusline"):
        prev = m.get("statusline_prev")
        if prev is None:
            try:
                with open(os.path.join(state_dir(), "statusline-chain.json"), encoding="utf-8") as f:
                    c = json.load(f)
                prev = dict(sl, command=c["command"])
            except (OSError, ValueError, KeyError):
                prev = None
        if prev:
            d["statusLine"] = prev
            print(f"  statusLine restored: {prev.get('command')}")
        else:
            del d["statusLine"]
    if d == {} and m.get("had_settings_file") is False and os.path.exists(path):
        os.remove(path)  # install created the file: remove it
        print(f"  removed {path} (created by install)")
    elif json.dumps(d, sort_keys=True) != before:
        save_settings(path, d)
        print(f"  cleaned {path}")
    else:
        print(f"  {path}: nothing of ours found")
    return 0


def installed() -> dict:
    """{"hooks": bool, "statusline": bool, "commands": [our hook commands], "legacy": bool} from Claude Code's
    settings; legacy: some of ours still use the pre-0.8.0 names (see is_legacy_command)."""
    out = {"hooks": False, "statusline": False, "commands": [], "legacy": False}
    try:
        d = load_settings(settings_path())
    except Exception:
        return out
    hooks = d.get("hooks") if isinstance(d.get("hooks"), dict) else {}
    for groups in hooks.values():
        for g in groups if isinstance(groups, list) else []:
            for h in (g.get("hooks") if isinstance(g, dict) and isinstance(g.get("hooks"), list) else []):
                if _is_ours(h, HOOK_MARK):
                    out["hooks"] = True
                    out["legacy"] = out["legacy"] or is_legacy_command(h.get("command", ""), "hook")
                    if h.get("command") not in out["commands"]:
                        out["commands"].append(h.get("command"))
    sl = d.get("statusLine")
    out["statusline"] = isinstance(sl, dict) and is_ours_command(sl.get("command", ""), "statusline")
    if out["statusline"] and is_legacy_command(sl.get("command", ""), "statusline"):
        out["legacy"] = True
    return out


def installed_matches_self(statusline: bool | None = None) -> bool:
    """True if our hooks are installed and all run the current program (hook_command()), and so does our
    statusLine wrapper if present. False when nothing is installed, or after the program moved (new AppImage
    path, .app copied to /Applications, repo moved): the app then re-runs install_for_app() on start.
    statusline=True also requires the wrapper; None (default) accepts it absent; False ignores it."""
    inst = installed()
    if not inst["hooks"] or inst["commands"] != [hook_command("hook")]:
        return False
    if statusline is False:
        return True
    try:
        sl = load_settings(settings_path()).get("statusLine")
    except Exception:
        return False
    cur = sl.get("command") if isinstance(sl, dict) and is_ours_command(sl.get("command", ""), "statusline") \
        else None
    if cur is None:
        return not statusline
    return cur == hook_command("statusline")


def check() -> int:
    """0 installed, 1 not installed, 2 settings.json unreadable, 3 installed under the pre-0.8.0 names (needs
    reinstall: `install` replaces those entries)."""
    try:
        load_settings(settings_path())
    except Exception:
        return 2
    inst = installed()
    if not inst["hooks"]:
        return 1
    return 3 if inst["legacy"] else 0


# ---------------------------------------------------------------------------------------------
# Antigravity global hooks (~/.gemini/config/hooks.json)
# ---------------------------------------------------------------------------------------------
def _antigravity_hook_entry(hook_cmd: str) -> dict:
    handler = {"type": "command", "command": hook_cmd, "timeout": 5}
    entry: dict = {"enabled": True}
    for ev in ANTIGRAVITY_FLAT_EVENTS:
        entry[ev] = [dict(handler)]
    for ev in ANTIGRAVITY_GROUPED_EVENTS:
        entry[ev] = [{"matcher": "", "hooks": [dict(handler)]}]
    return entry


def install_antigravity(hook_cmd: str | None = None) -> int:
    """Idempotently install SQRM8's observational hooks into Antigravity's global hooks.json."""
    cmd = hook_cmd or hook_command("hook", agent="antigravity")
    path = antigravity_hooks_path()
    existed = os.path.exists(path)
    d = load_settings(path)
    m = read_manifest()
    if "had_antigravity_hooks_file" not in m:
        m["had_antigravity_hooks_file"] = existed
    before = json.dumps(d, sort_keys=True)
    d[ANTIGRAVITY_HOOK_KEY] = _antigravity_hook_entry(cmd)
    m["antigravity_installed_at"] = int(time.time())
    m["antigravity_hook_command"] = cmd
    if json.dumps(d, sort_keys=True) != before:
        save_settings(path, d)
        print(f"  updated {path}")
    else:
        print(f"  {path} already up to date")
    write_manifest(m)
    return 0


def install_antigravity_script(root: str, python: str) -> int:
    cmd = script_command(python, os.path.join(root, "host", "hooks", HOOK_MARK), agent="antigravity")
    return install_antigravity(cmd)


def install_antigravity_for_app() -> int:
    return install_antigravity(hook_command("hook", agent="antigravity"))


def uninstall_antigravity() -> int:
    path = antigravity_hooks_path()
    if not os.path.exists(path):
        print(f"  {path}: nothing of ours found")
        return 0
    try:
        d = load_settings(path)
    except (ValueError, OSError) as e:
        print(f"error: cannot parse {path}: {e}; not touching it", file=sys.stderr)
        return 2
    m = read_manifest()
    before = json.dumps(d, sort_keys=True)
    d.pop(ANTIGRAVITY_HOOK_KEY, None)
    if d == {} and m.get("had_antigravity_hooks_file") is False and os.path.exists(path):
        os.remove(path)
        print(f"  removed {path} (created by install)")
    elif json.dumps(d, sort_keys=True) != before:
        save_settings(path, d)
        print(f"  cleaned {path}")
    else:
        print(f"  {path}: nothing of ours found")
    return 0


def antigravity_installed() -> dict:
    """{"hooks": bool, "commands": [our hook commands]} from Antigravity's global hooks.json."""
    out: dict = {"hooks": False, "commands": []}
    try:
        d = load_settings(antigravity_hooks_path())
    except Exception:
        return out
    entry = d.get(ANTIGRAVITY_HOOK_KEY)
    if not isinstance(entry, dict) or entry.get("enabled") is False:
        return out
    for ev in ANTIGRAVITY_FLAT_EVENTS:
        for h in (entry.get(ev) if isinstance(entry.get(ev), list) else []):
            if _is_ours(h, HOOK_MARK):
                out["hooks"] = True
                if h.get("command") not in out["commands"]:
                    out["commands"].append(h.get("command"))
    for ev in ANTIGRAVITY_GROUPED_EVENTS:
        for g in (entry.get(ev) if isinstance(entry.get(ev), list) else []):
            for h in (g.get("hooks") if isinstance(g, dict) and isinstance(g.get("hooks"), list) else []):
                if _is_ours(h, HOOK_MARK):
                    out["hooks"] = True
                    if h.get("command") not in out["commands"]:
                        out["commands"].append(h.get("command"))
    return out


def antigravity_installed_matches_self() -> bool:
    inst = antigravity_installed()
    return bool(inst["hooks"] and inst["commands"] == [hook_command("hook", agent="antigravity")])


def antigravity_check() -> int:
    """0 installed, 1 not installed, 2 hooks.json unreadable."""
    try:
        load_settings(antigravity_hooks_path())
    except Exception:
        return 2
    return 0 if antigravity_installed()["hooks"] else 1


# ---------------------------------------------------------------------------------------------
# Multi-agent orchestration
# ---------------------------------------------------------------------------------------------
def resolve_agents(agent: str = "auto") -> list[str]:
    a = (agent or "auto").strip().lower()
    if a == "all":
        return ["claude", "antigravity"]
    if a in ("claude", "antigravity"):
        return [a]
    # auto: Claude Code always (what `hooks install` did before there were other agents), the others when found
    return ["claude"] + [x for x in detected_agents() if x != "claude"]


def install_agents(agent: str = "auto", statusline: bool = True, root: str | None = None,
                   python: str | None = None) -> dict[str, int]:
    results: dict[str, int] = {}
    for target in resolve_agents(agent):
        if target == "claude":
            results["claude"] = (install(root, python, statusline) if (root and python)
                                 else install_for_app(statusline))
        elif target == "antigravity":
            try:
                results["antigravity"] = (install_antigravity_script(root, python) if (root and python)
                                          else install_antigravity_for_app())
            except (ValueError, OSError) as e:
                # its own file, its own failure: the Claude Code result above must survive it
                print(f"error: cannot update {antigravity_hooks_path()}: {e}", file=sys.stderr)
                results["antigravity"] = 2
    return results


def exit_code(results: dict[str, int], agent: str = "auto") -> int:
    """The process exit code for a set of per-agent results. In auto mode only Claude Code decides it: an
    optional agent that was merely detected must not fail an installer (tools/install.sh runs under `set -e`)
    or turn `hooks check` red for someone who never asked for it."""
    if (agent or "auto").strip().lower() == "auto" and "claude" in results:
        return results["claude"]
    return max(results.values()) if results else 0


def uninstall_agents(agent: str = "all") -> dict[str, int]:
    a = (agent or "all").strip().lower()
    targets = ["claude", "antigravity"] if a in ("all", "auto") else [a]
    results: dict[str, int] = {}
    for target in targets:
        if target == "claude":
            results["claude"] = uninstall()
        elif target == "antigravity":
            results["antigravity"] = uninstall_antigravity()
    return results


def status_all() -> dict[str, dict]:
    cc = installed()
    ag = antigravity_installed()
    return {
        "claude": {
            "detected": claude_detected(),
            "hooks": cc["hooks"],
            "statusline": cc["statusline"],
            "legacy": cc["legacy"],
            "commands": cc["commands"],
            "path": settings_path(),
        },
        "antigravity": {
            "detected": antigravity_detected(),
            "hooks": ag["hooks"],
            "commands": ag["commands"],
            "path": antigravity_hooks_path(),
        },
    }


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sqrm8_settings.py")
    ap.add_argument("action", choices=["install", "uninstall", "check"])
    ap.add_argument("--root")
    ap.add_argument("--python")
    ap.add_argument("--no-statusline", action="store_true")
    ap.add_argument("--agent", choices=["claude", "antigravity", "auto", "all"], default="claude")
    a = ap.parse_args(argv)
    try:
        from .hostos import migrate_state_dir   # tools/install.sh runs us first: keep the old state dir's data
        msg = migrate_state_dir()
        if msg:
            print(f"  {msg}")
    except ImportError:
        pass
    if a.action == "install":
        if not a.root or not a.python:
            ap.error("install needs --root and --python")
        try:
            res = install_agents(a.agent, not a.no_statusline, os.path.abspath(a.root), a.python)
            return max(res.values()) if res else 0
        except (ValueError, OSError) as e:
            print(f"error: cannot update hooks config: {e}", file=sys.stderr)
            return 2
    if a.action == "uninstall":
        res = uninstall_agents(a.agent)
        return max(res.values()) if res else 0
    if a.agent == "antigravity":
        return antigravity_check()
    if a.agent in ("auto", "all"):
        targets = resolve_agents(a.agent)
        rcs = [check() if t == "claude" else antigravity_check() for t in targets]
        return 0 if all(r == 0 for r in rcs) else max(rcs)
    rc = check()
    if rc == 3:
        print("hooks: installed under the old Claudius Maximus names: needs reinstall (run install)")
    return rc


if __name__ == "__main__":
    sys.exit(main())
