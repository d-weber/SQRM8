"""Client side of the hooks IPC: what Claude Code runs on every hook event and every status-line refresh.

MUST stay tiny, fast and silent: stdlib only, importable with `python -I -S`, no import of anything else in
`companion` (so `sqrm8 hook` in a frozen build, `python -m companion hook` and the standalone
host/hooks/sqrm8_hook.py / sqrm8_statusline.py wrappers all run this same code in a few tens of ms).

Transport (the daemon side is providers/hooks.py):
  * macOS/Linux: one AF_UNIX SOCK_DGRAM datagram to ~/.sqrm8/hooks.sock (or, when that path is
    too long for AF_UNIX, /tmp/sqrm8-<uid>-<crc>/hooks.sock: see unix_sock_path);
  * Windows (no AF_UNIX in CPython there): one UDP datagram to 127.0.0.1:<port>, where the daemon writes
    "<port> <key>\\n" to ~/.sqrm8/hooks.port. The key (random per daemon start) goes in the JSON as
    "k" so other local programs cannot inject fake hook events by guessing the port.
  Whichever file exists is used (hooks.sock first where AF_UNIX exists). No file = daemon not running = do
  nothing. Any error is swallowed.

hook_main(): reads the hook JSON from stdin, keeps only the few fields the companion needs (truncated) and sends
ONE datagram (<= 2000 bytes). Never prints to stdout (stdout of UserPromptSubmit/SessionStart hooks would be
injected into Claude's context) and the caller always exits 0.

Datagram format (JSON object, short keys):
    v=1, e=hook_event_name, ts=epoch float, sid=session_id, cwd, aid=agent_id, at=agent_type,
    tool=tool_name, inp={few tool_input keys, truncated}, tid=tool_use_id, msg=Notification message,
    nt=notification_type, prompt, last=last_assistant_message, err=error, src=source, why=reason,
    trig=trigger, intr=is_interrupt, title=session_title, model, k=key (UDP only)

statusline_main(): the statusLine wrapper. Claude Code pipes a JSON blob to the status line command after each
assistant message (and on its refresh timer). For claude.ai Pro/Max subscribers it contains the plan limits:
    "rate_limits": {"five_hour": {"used_percentage": 23.5, "resets_at": 1738425600},
                    "seven_day": {"used_percentage": 41.2, "resets_at": 1738857600}}
 1. writes the useful parts atomically to ~/.sqrm8/statusline.json (format documented in
    providers/hooks.py, read by providers/usage.py),
 2. sends a {"e":"StatusLine",...} datagram to the daemon (live per-session ctx%/cost/model),
 3. chains to the user's previous status line command (saved by the installer in
    ~/.sqrm8/statusline-chain.json as {"command": "..."}), feeding it the same stdin and printing
    its output, so the user's status line looks exactly as before.
Any failure in 1/2 is ignored; 3 always runs.
"""
import os
import sys
import time

MAXDGRAM = 2000
IS_WINDOWS = sys.platform == "win32"

INPUT_KEYS = ("command", "description", "file_path", "filePath", "path", "pattern", "url", "query", "prompt",
              "subagent_type", "skill", "notebook_path", "glob", "questions", "plan", "content")
AGY_INPUT_KEYS = ("CommandLine", "toolSummary", "Path", "TargetFile", "Query", "Url", "Role", "Prompt",
                  "Pattern", "DirectoryPath", "SearchPath", "steps", "questions")


def home():
    return (os.environ.get("SQRM8_HOME") or os.environ.get("CLAUDIUS_HOME")
            or os.path.join(os.path.expanduser("~"), ".sqrm8"))


SUN_PATH_MAX = 100   # sun_path is 104 bytes on macOS, 108 on Linux (incl. the NUL): keep a margin


def short_sock_dir(base):
    """Fallback dir for the socket when <state dir>/hooks.sock is too long for AF_UNIX (deep $HOME or
    $SQRM8_HOME): /tmp/sqrm8-<uid>-<crc of the state dir>, created 0700 by the daemon. The crc
    keeps two state dirs (tests, several checkouts) apart."""
    import zlib
    tag = "%08x" % (zlib.crc32(os.fsencode(os.path.abspath(base))) & 0xffffffff)
    return os.path.join("/tmp", "sqrm8-%d-%s" % (os.getuid(), tag))


def unix_sock_path(base):
    """The daemon's AF_UNIX socket path for state dir `base` (shared with providers/hooks.py)."""
    p = os.path.join(base, "hooks.sock")
    if IS_WINDOWS or len(os.fsencode(p)) <= SUN_PATH_MAX:
        return p
    return os.path.join(short_sock_dir(base), "hooks.sock")


def owned_by_me(path):
    """The fallback socket dir is in world-writable /tmp: only talk to it if it is ours (never leak hook
    data to a socket another user planted there)."""
    try:
        st = os.lstat(path)
        import stat
        return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and not st.st_mode & 0o077
    except (OSError, AttributeError):
        return False


def sock_path():
    return unix_sock_path(home())


def port_path():
    return os.path.join(home(), "hooks.port")


def read_port_file(path=None):
    """(port, key) from hooks.port, or None."""
    try:
        with open(path or port_path(), "rb") as f:
            parts = f.read(200).decode("ascii", "replace").split()
        port = int(parts[0])
        if not 0 < port < 65536:
            return None
        return port, (parts[1] if len(parts) > 1 else "")
    except (OSError, ValueError, IndexError):
        return None


def target():
    """Where to send: ("unix", path, "") | ("udp", ("127.0.0.1", port), key) | None (daemon not running)."""
    sp = sock_path()
    if not IS_WINDOWS and os.path.exists(sp):
        if os.path.dirname(sp) == home() or owned_by_me(os.path.dirname(sp)):
            return ("unix", sp, "")
    pk = read_port_file()
    if pk:
        return ("udp", ("127.0.0.1", pk[0]), pk[1])
    return None


def send(data, tgt):
    import socket
    if tgt[0] == "unix":
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    else:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setblocking(False)
    try:
        s.sendto(data, tgt[1])
    finally:
        s.close()


def cut(v, n):
    if v is None:
        return None
    if not isinstance(v, str):
        v = str(v)
    return v if len(v) <= n else v[:n]


def _parse_hook_argv(argv):
    """Parse optional `--agent <name>` and optional positional event fallback from hook argv."""
    agent = ""
    event = ""
    args = list(argv or [])
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--agent" and i + 1 < len(args):
            agent = str(args[i + 1]).strip().lower()
            i += 2
            continue
        if a.startswith("--agent="):
            agent = a.split("=", 1)[1].strip().lower()
            i += 1
            continue
        if not a.startswith("-") and not event:
            event = a
        i += 1
    return agent, event


def _compact_inputs(ti, keys=INPUT_KEYS):
    if not isinstance(ti, dict):
        return None
    inp = {}
    for k in keys:
        v = ti.get(k)
        if v is None:
            continue
        if k == "questions" and isinstance(v, list) and v:
            q = v[0] if isinstance(v[0], dict) else {}
            v = q.get("question") or q.get("header") or ""
        elif k == "steps" and isinstance(v, list):
            cur = next((s for s in v if isinstance(s, dict) and s.get("status") == "in_progress"), None)
            v = (cur.get("title") or "") if cur else f"{len(v)} steps"
        elif not isinstance(v, str):
            continue
        inp[k] = cut(v, 160)
    return inp or None


def _shrink_and_encode(r):
    import json
    data = json.dumps(r, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "replace")
    for k in ("inp", "last", "prompt", "msg", "err", "cwd", "title"):
        if len(data) <= MAXDGRAM:
            break
        if k in r:
            if k == "inp":
                r["inp"] = {ik: cut(iv, 60) for ik, iv in r["inp"].items()}
            else:
                r[k] = cut(r[k], 60)
            data = json.dumps(r, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "replace")
    while len(data) > MAXDGRAM and isinstance(r.get("inp"), dict) and r["inp"]:
        r["inp"].pop(next(reversed(r["inp"])))
        if not r["inp"]:
            del r["inp"]
        data = json.dumps(r, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "replace")
    return data if len(data) <= MAXDGRAM else None


def compact_antigravity(p, event="", key=""):
    """Antigravity camelCase hook JSON -> SQRM8 datagram bytes (<= MAXDGRAM)."""
    ev = p.get("hookEventName") or event
    sid = p.get("conversationId") or p.get("session_id")
    r = {"v": 1, "sa": "antigravity", "e": ev, "ts": round(time.time(), 3), "sid": sid}
    if key:
        r["k"] = key
    cwd = p.get("cwd")
    if not cwd:
        wps = p.get("workspacePaths")
        if isinstance(wps, list) and wps and isinstance(wps[0], str):
            cwd = wps[0]
    if cwd:
        r["cwd"] = cut(cwd, 300)
    for src, dst, n in (("modelName", "model", 60), ("lastUserInput", "prompt", 240),
                        ("error", "err", 200), ("terminationReason", "why", 60)):
        v = p.get(src)
        if v is not None and v != "":
            r[dst] = cut(v, n) if not isinstance(v, (bool, int, float)) else v
    tc = p.get("toolCall")
    if isinstance(tc, dict):
        if tc.get("name"):
            r["tool"] = cut(tc["name"], 80)
        if tc.get("id"):
            r["tid"] = cut(tc["id"], 80)
        inp = _compact_inputs(tc.get("args"), AGY_INPUT_KEYS + INPUT_KEYS)
        if inp:
            r["inp"] = inp
    return _shrink_and_encode(r)


def compact(p, event="", key="", agent=""):
    """Hook JSON -> datagram bytes (<= MAXDGRAM), or None if it can't be made to fit."""
    if agent == "antigravity" or (not agent and ("conversationId" in p or "hookEventName" in p)):
        return compact_antigravity(p, event, key)
    ev = p.get("hook_event_name") or event
    r = {"v": 1, "e": ev, "ts": round(time.time(), 3), "sid": p.get("session_id") or p.get("sessionID")}
    if agent and agent != "claude":
        r["sa"] = agent
    if key:
        r["k"] = key
    for src, dst, n in (("cwd", "cwd", 300), ("agent_id", "aid", 40), ("agent_type", "at", 60),
                        ("tool_name", "tool", 80), ("tool_use_id", "tid", 80), ("message", "msg", 200),
                        ("notification_type", "nt", 40), ("prompt", "prompt", 240),
                        ("last_assistant_message", "last", 240), ("error", "err", 200), ("source", "src", 30),
                        ("reason", "why", 60), ("trigger", "trig", 20), ("session_title", "title", 120),
                        ("model", "model", 60)):
        v = p.get(src)
        if v is not None and v != "":
            r[dst] = cut(v, n) if not isinstance(v, (bool, int, float)) else v
    if p.get("is_interrupt"):
        r["intr"] = True
    inp = _compact_inputs(p.get("tool_input"))
    if inp:
        r["inp"] = inp
    return _shrink_and_encode(r)


# ---------------------------------------------------------------------------------------------
# stdio that also works in a windowed (no console) frozen Windows build, where sys.stdin/stdout are None
# but Claude Code's pipes are still the process's standard handles.
# ---------------------------------------------------------------------------------------------
def _win_std_fd(which):
    import ctypes
    import msvcrt
    gsh = ctypes.windll.kernel32.GetStdHandle
    gsh.argtypes, gsh.restype = [ctypes.c_ulong], ctypes.c_void_p   # HANDLE is pointer-sized
    h = gsh(which & 0xFFFFFFFF)  # -10 stdin, -11 stdout (DWORD)
    if not h or h == ctypes.c_void_p(-1).value:  # NULL / INVALID_HANDLE_VALUE
        return None
    return msvcrt.open_osfhandle(h, (os.O_RDONLY if which == -10 else os.O_WRONLY) | getattr(os, "O_BINARY", 0))


def read_stdin():
    """All of stdin as bytes (always drained, so Claude Code never sees a broken pipe)."""
    stream = getattr(sys.stdin, "buffer", None)
    if stream is not None:
        return stream.read()
    if IS_WINDOWS:
        fd = _win_std_fd(-10)
        if fd is not None:
            with os.fdopen(fd, "rb") as f:
                return f.read()
    return b""


def write_stdout(data):
    stream = getattr(sys.stdout, "buffer", None)
    if stream is not None:
        stream.write(data)
        stream.flush()
        return
    if IS_WINDOWS:
        fd = _win_std_fd(-11)
        if fd is not None:
            os.write(fd, data)


# ---------------------------------------------------------------------------------------------
# entry points (callers wrap them: catch BaseException, exit 0)
# ---------------------------------------------------------------------------------------------
def hook_main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    agent, event = _parse_hook_argv(argv)
    raw = read_stdin()
    tgt = target()
    if tgt is None:  # daemon not running: cheapest possible exit
        return
    import json
    p = json.loads(raw.decode("utf-8", "replace"))
    if not isinstance(p, dict):
        return
    data = compact(p, event, tgt[2], agent=agent)
    if data:
        send(data, tgt)


def record(p):
    """statusline.json + the StatusLine datagram."""
    import json
    base = home()
    if not os.path.isdir(base):
        return
    now = time.time()
    path = os.path.join(base, "statusline.json")
    try:
        with open(path, encoding="utf-8") as f:
            prev = json.load(f)
        if not isinstance(prev, dict):
            prev = {}
    except Exception:
        prev = {}
    cw = p.get("context_window") or {}
    cost = p.get("cost") or {}
    model = p.get("model") or {}
    out = {
        "v": 1,
        "captured_at": int(now),
        "session_id": p.get("session_id"),
        "model": model.get("display_name") or model.get("id"),
        "model_id": model.get("id"),
        "version": p.get("version"),
        "ctx_pct": cw.get("used_percentage"),
        "ctx_size": cw.get("context_window_size"),
        "cost_usd": cost.get("total_cost_usd"),
        "rate_limits": prev.get("rate_limits"),
        "rate_limits_at": prev.get("rate_limits_at"),
    }
    rl = p.get("rate_limits")
    if isinstance(rl, dict) and rl:
        out["rate_limits"] = rl
        out["rate_limits_at"] = int(now)
    tmp = path + ".%d.tmp" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f)
    os.replace(tmp, path)

    tgt = target()
    if tgt and p.get("session_id"):
        d = {"v": 1, "e": "StatusLine", "ts": round(now, 3), "sid": p.get("session_id"),
             "model": out["model"], "ctx_pct": out["ctx_pct"], "cost_usd": out["cost_usd"],
             "cwd": (p.get("cwd") or "")[:300]}
        if tgt[2]:
            d["k"] = tgt[2]
        if isinstance(rl, dict) and rl:
            d["rl"] = rl
        data = json.dumps(d, separators=(",", ":")).encode()
        if len(data) <= MAXDGRAM:
            send(data, tgt)


def chained_command():
    import json
    cmd = os.environ.get("SQRM8_STATUSLINE_CHAIN") or os.environ.get("CLAUDIUS_STATUSLINE_CHAIN")
    if cmd:
        return cmd
    dirs = [home()]
    if not (os.environ.get("SQRM8_HOME") or os.environ.get("CLAUDIUS_HOME")):
        # the pre-0.8.0 state dir, in case moving it to ~/.sqrm8 failed: the user's status line must not vanish
        dirs.append(os.path.join(os.path.expanduser("~"), ".claudius-maximus"))
    for d in dirs:
        try:
            with open(os.path.join(d, "statusline-chain.json"), encoding="utf-8") as f:
                c = json.load(f)
            return c.get("command") if isinstance(c, dict) else None
        except Exception:
            continue
    return None


def _win_bash():
    """Git Bash, the shell Claude Code itself runs hook / status line commands with on Windows (so the user's
    previous statusLine command, written for it, keeps working): $CLAUDE_CODE_GIT_BASH_PATH, else the bash.exe
    of the Git for Windows that provides git on PATH. Never System32\\bash.exe (that is WSL). None: use cmd."""
    p = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")
    if p and os.path.isfile(p):
        return p
    import shutil
    git = shutil.which("git")
    if git:
        d = os.path.dirname(os.path.realpath(git))   # <Git>\cmd, <Git>\bin or <Git>\mingw64\bin
        for root in (os.path.dirname(d), os.path.dirname(os.path.dirname(d))):
            for rel in (("bin", "bash.exe"), ("usr", "bin", "bash.exe")):
                b = os.path.join(root, *rel)
                if os.path.isfile(b):
                    return b
    return None


def chain_argv(cmd):
    """How to run the chained command: (args, shell) for subprocess.run."""
    if IS_WINDOWS:
        bash = _win_bash()
        if bash:
            return [bash, "-c", cmd], False
    return cmd, True


def statusline_main(argv=None):
    import json
    raw = read_stdin()
    try:
        p = json.loads(raw.decode("utf-8", "replace"))
        if isinstance(p, dict):
            record(p)
    except Exception:
        pass
    cmd = chained_command()
    if cmd:
        try:
            import subprocess
            kw = {}
            if IS_WINDOWS:
                kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
                kw["stderr"] = subprocess.DEVNULL  # a windowed exe may have no stderr handle to pass on
            args, shell = chain_argv(cmd)
            r = subprocess.run(args, shell=shell, input=raw, stdout=subprocess.PIPE, timeout=10, **kw)
            write_stdout(r.stdout)
        except Exception:
            pass


def run_hook(argv=None):
    """`hook` entry point: run, swallow everything, flush, hard-exit 0 (skips interpreter teardown).
    argv: the arguments after the command (optional `--agent <name>` and event name fallback).
    Antigravity hooks expect a valid JSON object (`{}`) on stdout; Claude Code hooks must remain silent."""
    agent, _ = _parse_hook_argv(sys.argv[1:] if argv is None else argv)
    try:
        hook_main(argv)
    except BaseException:
        pass
    if agent == "antigravity":
        try:
            write_stdout(b"{}\n")
        except BaseException:
            pass
    try:
        sys.stdout.flush()
    except BaseException:
        pass
    os._exit(0)


def run_statusline():
    try:
        statusline_main()
    except BaseException:
        pass
    try:
        sys.stdout.flush()
    except BaseException:
        pass
    os._exit(0)
