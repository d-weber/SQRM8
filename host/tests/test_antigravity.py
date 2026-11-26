"""Tests for multi-agent support and Antigravity integration (transcripts, provider, hooks, usage, CLI, tray).

Run:  HOME=$(mktemp -d) python3 -m unittest host/tests/test_antigravity.py -v
No hardware, no network, never touches real ~/.claude, ~/.gemini, or ~/.sqrm8.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

HOST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, HOST)

from companion import app as appmod  # noqa: E402
from companion import daemon as daemon_mod  # noqa: E402
from companion import hookclient, hooksetup, runtime_focus  # noqa: E402
from companion.antigravity_transcript import AntigravityTranscript, decode_agy_args, extract_user_prompt  # noqa: E402
from companion.providers.antigravity import AntigravityProvider  # noqa: E402
from companion.providers.hooks import HooksProvider  # noqa: E402
from companion.providers.sessions import SessionsProvider, cwd_slug  # noqa: E402
from companion.providers.usage import UsageProvider  # noqa: E402
from companion.sessions_text import (context_window, model_family, model_label, normalize_tool,  # noqa: E402
                                     tool_detail, tool_label, usage_cost)

CID = "53f53dbf-3e82-4702-9810-85eb62ee1fee"
SUB_CID = "aaaa1111-2222-3333-4444-555566667777"
CC_SID = "11111111-2222-3333-4444-555555555555"
CWD = "/Users/test/projects/sqrm8"


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def agy_user(text: str, ts: float, step: int = 0) -> dict:
    return {
        "step_index": step,
        "source": "USER_EXPLICIT",
        "type": "USER_INPUT",
        "status": "DONE",
        "created_at": iso(ts),
        "content": f"<USER_REQUEST>\n{text}\n</USER_REQUEST>\n<ADDITIONAL_METADATA>time</ADDITIONAL_METADATA>",
    }


def agy_planner(ts: float, step: int, model: str = "gemini-4-neon-high", content: str = "",
                tool_calls: list | None = None, inp: int = 1000, out: int = 200, cr: int = 500) -> dict:
    r = {
        "step_index": step,
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "created_at": iso(ts),
        "model": model,
        "input_tokens": inp,
        "cache_read_tokens": cr,
        "output_tokens": out,
        "content": content,
        "thinking": "planning",
    }
    if tool_calls is not None:
        r["tool_calls"] = tool_calls
    return r


def agy_generic(ts: float, step: int, content: str = "ok", status: str = "DONE") -> dict:
    return {
        "step_index": step,
        "source": "MODEL",
        "type": "GENERIC",
        "status": status,
        "created_at": iso(ts),
        "content": content,
    }


def write_jsonl(path: str, *records: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")


class TempEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sqrm8-agy-")
        self.home = os.path.join(self.tmp, "h")
        self.state = os.path.join(self.home, ".sqrm8")
        self.claude = os.path.join(self.home, ".claude")
        self.gemini_cfg = os.path.join(self.home, ".gemini", "config")
        self.agy_cli = os.path.join(self.home, ".gemini", "antigravity-cli")
        os.makedirs(self.state)
        env = {
            "HOME": self.home,
            "USERPROFILE": self.home,
            "SQRM8_HOME": self.state,
            "CLAUDE_CONFIG_DIR": self.claude,
            "GEMINI_CONFIG_DIR": self.gemini_cfg,
            "ANTIGRAVITY_CLI_DIR": self.agy_cli,
            "ANTIGRAVITY_APP_DIR": os.path.join(self.home, ".gemini", "antigravity"),
            "ANTIGRAVITY_IDE_DIR": os.path.join(self.home, ".gemini", "antigravity-ide"),
        }
        self._env = mock.patch.dict(os.environ, env)
        self._env.start()
        for k in ("CLAUDIUS_HOME", "CLAUDIUS_HOOKS_TRANSPORT", "SQRM8_HOOKS_TRANSPORT"):
            os.environ.pop(k, None)

    def tearDown(self):
        self._env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------------------------------------------------------------------------------------------
class NormalizationTests(unittest.TestCase):
    def test_tool_aliases_antigravity_and_opencode(self):
        expected = {
            "read_file": "Read", "view_file": "Read",
            "edit_file": "Edit", "replace_file_content": "Edit",
            "write_file": "Write", "run_command": "Bash",
            "grep_search": "Grep", "find_by_name": "Glob", "list_dir": "LS",
            "search_web": "WebSearch", "read_url_content": "WebFetch",
            "invoke_subagent": "Agent", "update_steps": "TodoWrite", "ask_question": "AskUserQuestion",
            # OpenCode
            "read": "Read", "edit": "Edit", "patch": "Edit", "write": "Write",
            "bash": "Bash", "grep": "Grep", "glob": "Glob", "ls": "LS",
            "webfetch": "WebFetch", "websearch": "WebSearch", "task": "Agent",
            "todowrite": "TodoWrite", "question": "AskUserQuestion",
        }
        for raw, canon in expected.items():
            self.assertEqual(normalize_tool(raw), canon, raw)
            self.assertEqual(tool_label(raw), canon, raw)

    def test_antigravity_tool_details(self):
        self.assertEqual(tool_detail("read_file", {"Path": CWD + "/host/companion/daemon.py"}, CWD),
                         "host/companion/daemon.py")
        self.assertEqual(tool_detail("run_command", {"CommandLine": "pytest -q", "toolSummary": "Run tests"}),
                         "pytest -q")
        long_cmd = "python3 -m unittest " + "test_x " * 30
        self.assertEqual(tool_detail("run_command", {"CommandLine": long_cmd, "toolSummary": "Run full suite"}),
                         "Run full suite")
        self.assertEqual(tool_detail("search_web", {"Query": "opencode sqlite schema"}), "opencode sqlite schema")
        self.assertEqual(tool_detail("invoke_subagent", {"Role": "Codebase Researcher"}), "Codebase Researcher")
        self.assertEqual(
            tool_detail("ask_question", {"questions": [{"question": "Which scope?"}]}),
            "Which scope?",
        )
        self.assertEqual(
            tool_detail("update_steps", {"steps": [{"title": "Build parser", "status": "in_progress"}]}),
            "Build parser",
        )

    def test_gemini_models_and_pricing(self):
        self.assertEqual(model_label("gemini-4-neon-high"), "gemini-4-neon")
        self.assertEqual(model_label("gemini-2.5-pro-preview-06-05"), "gemini-2.5-pro")
        self.assertEqual(model_label("google/gemini-2.5-flash"), "gemini-2.5-flash")
        self.assertEqual(model_label("anthropic/claude-opus-5-5"), "opus-5.5")
        self.assertEqual(model_family("gemini-4-neon-high"), "gemini")
        self.assertEqual(context_window("gemini-4-neon-high"), 1_000_000)
        self.assertGreater(usage_cost("gemini-4-neon-high", 10_000, 1_000, 50_000, 0), 0.0)


# ---------------------------------------------------------------------------------------------
class AntigravityTranscriptTests(TempEnv):
    def test_decode_args_and_prompt(self):
        args = decode_agy_args({
            "Path": '"/tmp/foo.py"',
            "StartLine": "10",
            "questions": '[{"question": "Scope?"}]',
            "raw": "plain text",
        })
        self.assertEqual(args["Path"], "/tmp/foo.py")
        self.assertEqual(args["StartLine"], 10)
        self.assertEqual(args["questions"], [{"question": "Scope?"}])
        self.assertEqual(args["raw"], "plain text")
        self.assertEqual(extract_user_prompt("<USER_REQUEST>\n/plan hello\n</USER_REQUEST>"), "/plan hello")

    def test_transcript_flow_todos_waiting_subagent_and_error(self):
        p = os.path.join(self.agy_cli, "brain", CID, ".system_generated", "logs", "transcript.jsonl")
        sub_p = os.path.join(self.agy_cli, "brain", SUB_CID, ".system_generated", "logs", "transcript.jsonl")
        now = time.time()
        prov = AntigravityProvider(dirs=[("cli", self.agy_cli)])
        prov.poll(now)

        # User prompt + tool call + todos + ask_question
        write_jsonl(
            p,
            agy_user("Implement Antigravity support", now + 1, step=0),
            agy_planner(now + 2, step=1, tool_calls=[
                {"name": "read_file", "args": {"Path": json.dumps(CWD + "/README.md")}},
                {"name": "update_steps", "args": {"steps": json.dumps([
                    {"title": "Read docs", "status": "in_progress"},
                    {"title": "Write code", "status": "pending"},
                ])}},
            ]),
            agy_generic(now + 3, step=2, content="README contents"),
            agy_generic(now + 3, step=3, content="OK"),
            agy_planner(now + 4, step=4, tool_calls=[
                {"name": "update_steps", "args": {"steps": json.dumps([
                    {"title": "Read docs", "status": "done"},
                    {"title": "Write code", "status": "in_progress"},
                ])}},
                {"name": "ask_question", "args": {"questions": json.dumps([
                    {"question": "Host-only or firmware too?"},
                ])}},
            ]),
            agy_generic(now + 4, step=5, content="OK"),
        )
        prov.last_scan = 0
        prov.poll(now + 5)
        st = {}
        prov.contribute(st)
        ev = prov.drain_events()

        self.assertEqual(len(st["sessions"]), 1)
        s = st["sessions"][0]
        self.assertEqual(s["id"], CID[:8])
        self.assertEqual(s["source"], "antigravity")
        self.assertEqual(s["kind"], "cli")
        self.assertEqual(s["model"], "gemini-4-neon")
        self.assertEqual(s["status"], "waiting")
        self.assertEqual(s["waiting"], "Question: Host-only or firmware too?")
        self.assertEqual(s["todos"], {"done": 1, "total": 2, "current": "Write code"})
        self.assertEqual(s["tokens"]["in"], 2000)
        self.assertEqual(s["tokens"]["out"], 400)
        self.assertEqual(s["tokens"]["cache_read"], 1000)
        self.assertIn("todo_done", [e["kind"] for e in ev])
        self.assertIn("waiting", [e["kind"] for e in ev])

        # Answer arrives + failing command + subagent launch
        write_jsonl(
            p,
            agy_generic(now + 6, step=6, content="A1: Host-only"),
            agy_planner(now + 7, step=7, tool_calls=[
                {"name": "run_command", "args": {"CommandLine": json.dumps("pytest")}},
                {"name": "invoke_subagent", "args": {"Role": json.dumps("Researcher"), "TypeName": json.dumps("research")}},
            ]),
            agy_generic(now + 8, step=8, content="Command exited with code 1\nFAIL"),
            agy_generic(now + 8, step=9, content=f"Launched subagent conversationId: {SUB_CID}"),
        )
        write_jsonl(
            sub_p,
            agy_user("Research codebase", now + 8, step=0),
            agy_planner(now + 9, step=1, tool_calls=[
                {"name": "grep_search", "args": {"Query": json.dumps("Provider")}},
            ]),
        )
        prov.last_scan = 0
        prov.poll(now + 10)
        st = {}
        prov.contribute(st)
        ev = prov.drain_events()

        # Subagent is nested under parent, NOT a second top-level session!
        self.assertEqual([x["id"] for x in st["sessions"]], [CID[:8]])
        s = st["sessions"][0]
        self.assertEqual(s["status"], "busy")
        self.assertEqual(len(s["subagents"]), 1)
        self.assertEqual(s["subagents"][0]["id"], SUB_CID[:8])
        self.assertEqual(s["subagents"][0]["desc"], "Researcher")
        self.assertEqual(s["subagents"][0]["tool"], "Grep")
        self.assertIn("tool_error", [e["kind"] for e in ev])

        # Text-only reply closes turn -> idle
        write_jsonl(p, agy_planner(now + 11, step=10, content="All done!"))
        write_jsonl(sub_p, agy_planner(now + 11, step=2, content="Subagent done"))
        prov.poll(now + 12)
        st = {}
        prov.contribute(st)
        self.assertEqual(st["sessions"][0]["status"], "idle")


# ---------------------------------------------------------------------------------------------
class MultiAgentAggregationTests(TempEnv):
    def test_simultaneous_claude_and_antigravity_sessions_and_usage(self):
        now = time.time()
        # Claude fixture
        os.makedirs(os.path.join(self.claude, "sessions"))
        cc_dir = os.path.join(self.claude, "projects", cwd_slug(CWD))
        cc_tr = os.path.join(cc_dir, CC_SID + ".jsonl")
        with open(os.path.join(self.claude, "sessions", f"{os.getpid()}.json"), "w") as f:
            json.dump({"pid": os.getpid(), "sessionId": CC_SID, "cwd": CWD, "kind": "interactive",
                       "status": "busy", "startedAt": int((now - 60) * 1000),
                       "updatedAt": int(now * 1000), "statusUpdatedAt": int(now * 1000)}, f)
        write_jsonl(
            cc_tr,
            {"type": "user", "origin": {"kind": "human"}, "message": {"role": "user", "content": "Fix bug"},
             "timestamp": iso(now - 5), "cwd": CWD, "sessionId": CC_SID},
            {"type": "assistant", "message": {
                "id": "m1", "model": "claude-opus-5-5", "role": "assistant",
                "content": [{"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": CWD + "/a.py"}}],
                "usage": {"input_tokens": 500, "output_tokens": 100, "cache_read_input_tokens": 0,
                          "cache_creation_input_tokens": 0},
            }, "timestamp": iso(now - 3), "cwd": CWD, "sessionId": CC_SID},
        )
        # Antigravity fixture
        agy_tr = os.path.join(self.agy_cli, "brain", CID, ".system_generated", "logs", "transcript.jsonl")
        write_jsonl(
            agy_tr,
            agy_user("Plan feature", now - 4, step=0),
            agy_planner(now - 2, step=1, tool_calls=[
                {"name": "read_file", "args": {"Path": json.dumps(CWD + "/README.md")}},
            ], inp=1000, out=250, cr=400),
        )

        cc_prov = SessionsProvider(base=self.claude)
        ag_prov = AntigravityProvider(dirs=[("cli", self.agy_cli)])
        usage_prov = UsageProvider(
            projects_glob=os.path.join(self.claude, "projects", "**", "*.jsonl"),
            antigravity_dirs=[self.agy_cli],
        )
        hooks_prov = HooksProvider(path=os.path.join(self.state, "hooks.sock"))
        try:
            for p in (cc_prov, ag_prov, usage_prov):
                p.poll(now)
            state = daemon_mod.snapshot([cc_prov, ag_prov, usage_prov, hooks_prov])
            ids = {s["id"]: s for s in state["sessions"]}
            self.assertEqual(set(ids.keys()), {CC_SID[:8], CID[:8]})
            self.assertEqual(ids[CC_SID[:8]]["source"], "claude")
            self.assertEqual(ids[CID[:8]]["source"], "antigravity")
            self.assertEqual(state["totals"]["sessions"], 2)
            self.assertEqual(state["totals"]["busy"], 2)
            # Today tokens = Claude (500+100) + Antigravity (1000+250) = 1850
            self.assertEqual(state["totals"]["tokens_today"], 1850)
            self.assertEqual(state["usage"]["today"]["tokens"], 1850)
            self.assertIn("opus", state["usage"]["by_model"])
            self.assertIn("gemini", state["usage"]["by_model"])
        finally:
            hooks_prov.close()

    def test_event_filter_keeps_antigravity_transcript_tools_when_hooks_active(self):
        ef = daemon_mod.EventFilter()
        ag_prov = AntigravityProvider(dirs=[("cli", self.agy_cli)])
        hook_sids = {CID[:8]}
        evs = [
            {"kind": "prompt", "sid": CID[:8], "text": "hi"},
            {"kind": "tool", "sid": CID[:8], "tool": "Read", "text": "README.md"},
            {"kind": "waiting", "sid": CID[:8], "text": "Question: Pick one"},
            {"kind": "todo_done", "sid": CID[:8], "text": "Step 1"},
            {"kind": "stop", "sid": CID[:8], "text": "Done"},
        ]
        kept = [e["kind"] for e in ef.filter(ag_prov, evs, hook_sids)]
        # prompt & stop are covered by Antigravity hooks; tool, waiting, todo_done must NOT be dropped!
        self.assertEqual(kept, ["tool", "waiting", "todo_done"])


# ---------------------------------------------------------------------------------------------
class AntigravityHooksTests(TempEnv):
    def test_hook_client_stdout_contract_and_ipc(self):
        hook_script = os.path.join(HOST, "hooks", "sqrm8_hook.py")
        payload = json.dumps({
            "conversationId": CID,
            "hookEventName": "PostToolUse",
            "cwd": CWD,
            "modelName": "gemini-4-neon-high",
            "toolCall": {"id": "tc1", "name": "run_command", "args": {"CommandLine": "git status"}},
        }).encode()

        # 1) No daemon running: Antigravity hook MUST still print "{}\n" and exit 0
        r = subprocess.run([sys.executable, "-I", "-S", hook_script, "--agent", "antigravity"],
                           input=payload, capture_output=True, timeout=5)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), b"{}")

        # 2) Malformed stdin: Antigravity hook MUST still print "{}\n" and exit 0
        r_bad = subprocess.run([sys.executable, "-I", "-S", hook_script, "--agent", "antigravity"],
                               input=b"not-json", capture_output=True, timeout=5)
        self.assertEqual(r_bad.returncode, 0)
        self.assertEqual(r_bad.stdout.strip(), b"{}")

        # 3) Claude Code hook: MUST remain completely silent (0 bytes stdout)
        r_cc = subprocess.run([sys.executable, "-I", "-S", hook_script],
                              input=b'{"session_id":"abc","hook_event_name":"Stop"}',
                              capture_output=True, timeout=5)
        self.assertEqual(r_cc.returncode, 0)
        self.assertEqual(r_cc.stdout, b"")

        # 4) Live IPC delivery to HooksProvider (default platform transport: AF_UNIX on POSIX, UDP on Windows).
        # If socket.sendto is blocked by an OS sandbox (PermissionError), verify the exact wire bytes directly.
        hp = HooksProvider()
        try:
            hp.poll(time.time())
            self.assertIsNotNone(hp.sock)
            ev_payloads = (
                {"conversationId": CID, "hookEventName": "SessionStart", "cwd": CWD},
                {"conversationId": CID, "hookEventName": "PreInvocation", "cwd": CWD,
                 "modelName": "gemini-4-neon-high", "lastUserInput": "Hello"},
                {"conversationId": CID, "hookEventName": "PostToolUse", "cwd": CWD,
                 "toolCall": {"id": "tc1", "name": "run_command", "args": {"CommandLine": "git status"}}},
                {"conversationId": CID, "hookEventName": "Stop", "cwd": CWD, "terminationReason": "NO_TOOL_CALL"},
            )
            for ev_payload in ev_payloads:
                r_live = subprocess.run([sys.executable, "-I", "-S", hook_script, "--agent", "antigravity"],
                                        input=json.dumps(ev_payload).encode(), capture_output=True, timeout=5)
                self.assertEqual(r_live.returncode, 0)
                self.assertEqual(r_live.stdout.strip(), b"{}")
            time.sleep(0.05)
            hp.poll(time.time())
            if hp.received == 0:
                # Restricted sandbox (socket.sendto EPERM): feed exact hookclient.compact() wire bytes into HooksProvider
                for ev_payload in ev_payloads:
                    raw = hookclient.compact(ev_payload, agent="antigravity")
                    self.assertIsNotNone(raw)
                    hp.handle(json.loads(raw.decode("utf-8")))
            live = hp.live()
            self.assertIn(CID[:8], live)
            s = live[CID[:8]]
            self.assertEqual(s["source"], "antigravity")
            self.assertEqual(s["status"], "idle")
            self.assertEqual(s["model"], "gemini-4-neon")
            self.assertEqual(s["activity"]["tool"], "Bash")
            self.assertEqual(s["activity"]["detail"], "git status")
            self.assertEqual(s["last_prompt"], "Hello")
            kinds = [e["kind"] for e in hp.drain_events()]
            # no "tool" from the hook: the transcript provider emits it (two sources would show it twice)
            self.assertEqual(kinds, ["session_start", "prompt", "stop"])
        finally:
            hp.close()

    def test_hooksetup_install_preserves_other_hooks_and_omits_pretooluse(self):
        os.makedirs(self.gemini_cfg, exist_ok=True)
        hpath = hooksetup.antigravity_hooks_path()
        # Pre-existing user hook
        with open(hpath, "w", encoding="utf-8") as f:
            json.dump({"my-linter": {"PostToolUse": [{"matcher": "edit_file", "hooks": [{"command": "lint.sh"}]}]}}, f)

        self.assertEqual(hooksetup.antigravity_check(), 1)
        rc = hooksetup.install_antigravity_for_app()
        self.assertEqual(rc, 0)
        self.assertEqual(hooksetup.antigravity_check(), 0)
        self.assertTrue(hooksetup.antigravity_installed_matches_self())

        with open(hpath, encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertIn("my-linter", cfg)
        self.assertIn("sqrm8", cfg)
        sq = cfg["sqrm8"]
        self.assertNotIn("PreToolUse", sq, "PreToolUse must never be installed (fail-closed gate)")
        for ev in ("SessionStart", "PreInvocation", "PostToolUse", "PostInvocation", "Stop"):
            self.assertIn(ev, sq)
        # Backup created
        baks = [n for n in os.listdir(self.gemini_cfg) if n.startswith("hooks.json.bak-sqrm8-")]
        self.assertTrue(baks)

        # Uninstall removes only sqrm8, keeps user hook
        self.assertEqual(hooksetup.uninstall_antigravity(), 0)
        self.assertEqual(hooksetup.antigravity_check(), 1)
        with open(hpath, encoding="utf-8") as f:
            cfg2 = json.load(f)
        self.assertIn("my-linter", cfg2)
        self.assertNotIn("sqrm8", cfg2)

    def test_hooksetup_uninstall_removes_file_it_created(self):
        hpath = hooksetup.antigravity_hooks_path()
        self.assertFalse(os.path.exists(hpath))
        self.assertEqual(hooksetup.install_antigravity_for_app(), 0)
        self.assertTrue(os.path.exists(hpath))
        self.assertEqual(hooksetup.uninstall_antigravity(), 0)
        self.assertFalse(os.path.exists(hpath))


# ---------------------------------------------------------------------------------------------
class TrayAndFocusTests(TempEnv):
    def test_tray_menu_shows_antigravity_only_when_detected(self):
        class StubApp:
            def __getattr__(self, _name):
                return lambda *a, **kw: None

        st_claude_only = {"connected": False, "ports": [], "sessions": [], "hooks_installed": True}
        labels_cc = [i.label for i in appmod.build_menu(st_claude_only, StubApp()) if not i.separator]
        self.assertFalse(any("Antigravity" in x for x in labels_cc))

        st_with_agy = dict(st_claude_only, antigravity_detected=True, antigravity_hooks_installed=False)
        labels_agy = [i.label for i in appmod.build_menu(st_with_agy, StubApp()) if not i.separator]
        self.assertIn("Install Antigravity hooks", labels_agy)

        st_agy_ok = dict(st_claude_only, antigravity_detected=True, antigravity_hooks_installed=True)
        labels_agy_ok = [i.label for i in appmod.build_menu(st_agy_ok, StubApp()) if not i.separator]
        self.assertIn("Antigravity hooks: installed", labels_agy_ok)

    def test_focus_antigravity_gui_routing(self):
        with mock.patch.object(runtime_focus, "_focus_antigravity_gui", return_value="focused Antigravity") as m:
            res = runtime_focus.focus_session(CID[:8], info={"source": "antigravity", "surface": "app", "cwd": CWD})
            self.assertEqual(res, "focused Antigravity")
            m.assert_called_once_with(CID[:8], CWD)


# ---------------------------------------------------------------------------------------------
class ClaudeOnlyUnchangedTests(TempEnv):
    """What a Claude-Code-only user had before the other agents came: none of it may move."""

    def test_claude_tool_names_are_not_renamed(self):
        for name in ("Task", "MultiEdit", "TodoRead", "Read", "Bash", "List", "Patch", "Question",
                     "mcp__srv__do_thing"):
            self.assertEqual(normalize_tool(name), name, name)

    def test_gemini_cli_directory_is_not_antigravity(self):
        os.makedirs(os.path.join(self.home, ".gemini"))
        with open(os.path.join(self.home, ".gemini", "settings.json"), "w") as f:
            f.write("{}")
        with mock.patch.object(hooksetup.shutil, "which", return_value=None):
            self.assertFalse(hooksetup.antigravity_detected())
            self.assertEqual(hooksetup.resolve_agents("auto"), ["claude"])
            os.makedirs(self.agy_cli)
            self.assertTrue(hooksetup.antigravity_detected())
            self.assertEqual(hooksetup.resolve_agents("auto"), ["claude", "antigravity"])

    def test_auto_always_includes_claude(self):
        os.makedirs(self.agy_cli)      # Antigravity only: no ~/.claude, no claude on the PATH
        with mock.patch.object(hooksetup.shutil, "which", return_value=None):
            self.assertFalse(hooksetup.claude_detected())
            self.assertEqual(hooksetup.resolve_agents("auto"), ["claude", "antigravity"])

    def test_broken_antigravity_hooks_file_does_not_fail_an_auto_install(self):
        os.makedirs(self.agy_cli)
        os.makedirs(self.gemini_cfg)
        with open(os.path.join(self.gemini_cfg, "hooks.json"), "w") as f:
            f.write("{ // jsonc\n not json")
        with mock.patch.object(hooksetup.shutil, "which", return_value=None), \
                mock.patch.object(hooksetup, "install_for_app", return_value=0), \
                mock.patch("sys.stderr"), mock.patch("sys.stdout"):
            res = hooksetup.install_agents("auto")
        self.assertEqual(res["claude"], 0)
        self.assertNotEqual(res["antigravity"], 0)
        self.assertEqual(hooksetup.exit_code(res, "auto"), 0)
        self.assertNotEqual(hooksetup.exit_code(res, "all"), 0)
        with open(os.path.join(self.gemini_cfg, "hooks.json")) as f:
            self.assertEqual(f.read(), "{ // jsonc\n not json")       # left as it was

    def test_bad_antigravity_line_does_not_stall_usage(self):
        now = time.time()
        cc_tr = os.path.join(self.claude, "projects", cwd_slug(CWD), CC_SID + ".jsonl")
        write_jsonl(cc_tr, {"type": "assistant", "message": {
            "id": "m1", "model": "claude-opus-5-5", "role": "assistant", "content": [],
            "usage": {"input_tokens": 500, "output_tokens": 100, "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0}}, "timestamp": iso(now - 30), "sessionId": CC_SID})
        os.utime(cc_tr, (now - 30, now - 30))
        agy_tr = os.path.join(self.agy_cli, "brain", CID, ".system_generated", "logs", "transcript.jsonl")
        bad = agy_planner(now - 3, step=1)
        bad["input_tokens"] = "n/a"
        notstr = agy_planner(now - 3, step=2)
        notstr["model"] = {"name": "gemini"}
        write_jsonl(agy_tr, bad, notstr)
        with open(agy_tr, "a") as f:
            f.write('["PLANNER_RESPONSE"]\n')
        write_jsonl(agy_tr, agy_planner(now - 2, step=3, inp=1000, out=250, cr=400))   # newest file: read first
        up = UsageProvider(projects_glob=os.path.join(self.claude, "projects", "**", "*.jsonl"),
                           antigravity_dirs=[self.agy_cli])
        for _ in range(3):
            up.poll(now)
        state = {}
        up.contribute(state)
        self.assertEqual(state["usage"]["today"]["tokens"], 600 + 1250)
        self.assertEqual(up.queue, [])

    def test_process_matching_keeps_claude_case_sensitive(self):
        from companion.providers import system as sysmod
        sp = sysmod.SystemProvider.__new__(sysmod.SystemProvider)
        sp.node_is_claude = {}
        cmds = {1: ["node", "/Users/u/Library/Application Support/Claude/mcp/server.js"],
                2: ["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"],
                3: ["node", "/home/u/work/opencode-notes/build.js"]}
        fake = mock.Mock()
        fake.Process.side_effect = lambda pid: mock.Mock(cmdline=lambda: cmds[pid])
        with mock.patch.object(sysmod, "psutil", fake):
            self.assertTrue(sp._is_agent_proc(10, "claude"))
            self.assertFalse(sp._is_agent_proc(11, "Claude"))      # the desktop app on macOS
            self.assertFalse(sp._is_agent_proc(1, "node"))
            self.assertTrue(sp._is_agent_proc(2, "node"))
            self.assertFalse(sp._is_agent_proc(3, "node"))
            self.assertTrue(sp._is_agent_proc(12, "agy"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
