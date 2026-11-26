"""Incremental Antigravity transcript (.jsonl) reader + per-transcript state.

Transcript facts verified on live Antigravity CLI/App/IDE logs
(`~/.gemini/{antigravity-cli,antigravity,antigravity-ide}/brain/<cid>/.system_generated/logs/transcript.jsonl`):
  * One JSON object per line with `step_index`, `source`, `type`, `status`, `created_at`.
  * `type == "USER_INPUT"` (source `USER_EXPLICIT`): human prompt wrapped in `<USER_REQUEST>...</USER_REQUEST>`.
  * `type == "PLANNER_RESPONSE"` (source `MODEL`): carries `model`, `input_tokens`, `cache_read_tokens`,
    `output_tokens`, `content`, `thinking`, and optional `tool_calls: [{"name": ..., "args": {...}}]`.
    Values inside `tool_calls[*].args` are JSON-encoded strings (e.g. `"\"/path\""`, `"[{\"question\":...}]"`).
  * `type == "GENERIC"` (source `MODEL`): tool result step, one per tool call in `tool_calls` order.
  * Special tools: `update_steps` -> todos + `todo_done`, `ask_question` -> `waiting` state,
    `invoke_subagent` -> child subagent conversation UUID in the following `GENERIC` result.
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import deque

from .sessions_text import (ASK_TOOLS, ascii_text, context_window, normalize_tool, parse_ts, plain, tool_detail,
                            tool_label, usage_cost)
from .sessions_transcript import Tail

_USER_REQ = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.S)
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_EXIT_ERR = re.compile(r"Command exited with code ([1-9]\d*)")
_CWD_HINT = re.compile(r"(?:Command Working Directory|Active Workspaces):\s*(?:-\s*)?(/[^\n\r]+|[A-Za-z]:\\[^\n\r]+)")


def _first_line(s: str) -> str:
    for line in (s or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def decode_agy_args(args) -> dict:
    """Antigravity serializes each value in `tool_calls[*].args` as a JSON string. Decode cleanly."""
    if not isinstance(args, dict):
        return {}
    out = {}
    for k, v in args.items():
        if isinstance(v, str):
            s = v.strip()
            if s and (s[0] in ('"', "[", "{") or s in ("true", "false", "null") or s[0].isdigit() or s[0] == "-"):
                try:
                    out[k] = json.loads(s)
                    continue
                except Exception:
                    pass
        out[k] = v
    return out


def extract_user_prompt(content: str) -> str:
    """Extract the user's text from an Antigravity USER_INPUT record."""
    if not content:
        return ""
    hits = _USER_REQ.findall(content)
    if hits:
        for h in reversed(hits):
            s = h.strip()
            if s:
                return s
    # fallback: drop leading metadata blocks if tags aren't present
    txt = re.sub(r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", "", content, flags=re.S)
    txt = re.sub(r"<USER_SETTINGS_CHANGE>.*?</USER_SETTINGS_CHANGE>", "", txt, flags=re.S)
    return txt.strip()


def _infer_project_root(path: str) -> str:
    """Best-effort workspace root guess from a file path when no hook cwd has arrived yet."""
    if not path or not isinstance(path, str):
        return ""
    # Skip Antigravity's own app data / skill paths when inferring project cwd
    low = path.replace("\\", "/").lower()
    if "/.gemini/" in low or low.startswith("/tmp/"):
        return ""
    d = path if os.path.isdir(path) else os.path.dirname(path)
    cur = d
    for _ in range(8):
        if not cur or cur == os.path.dirname(cur):
            break
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        cur = os.path.dirname(cur)
    return d


class AntigravityTranscript:
    """State derived from one Antigravity `transcript.jsonl` file (main conversation or subagent)."""

    def __init__(self, path: str, cid: str, is_sub: bool = False, emit_after: float = 0.0):
        self.path = path
        self.sid = cid                     # full conversation UUID
        self.is_sub = is_sub
        self.agent_id = cid if is_sub else ""
        self.tail = Tail(path)
        self.emit_after = emit_after
        self.events: list[tuple[str, dict]] = []
        self.cwd = ""
        self.cwd_inferred = False
        self.title = ""
        self.last_prompt = ""
        self.last_prompt_ts = 0.0
        self.model = ""
        self.first_ts = 0.0
        self.last_ts = 0.0
        self.last_activity_ts = 0.0
        self.tokens = [0, 0, 0, 0]         # in, out, cache_read, cache_write
        self.cost = 0.0
        self.ctx_tokens = 0
        self.ctx_model = ""
        self.tool_count = 0
        self.activity = {"tool": "", "detail": "", "ts": 0}
        self.last_text = ""
        self.turn_open = False
        self.turn_text = ""
        self.stop_pending = None           # (step_idx, ts, observed wall time)
        self.turn_end_ts = 0.0
        self.pending: deque[tuple[str, str, str, float, dict]] = deque()
        self.todos: list[dict] = []
        self.child_agents: dict[str, dict] = {}   # child cid -> {role, type, started}
        self._seen_steps: set[int] = set()
        self.day = ""
        self.today = [0, 0.0, 0, 0]        # tokens (in+out+cache_write), cost, tools, prompts

    # ------------------------------------------------------------------ io
    def poll(self, budget_bytes: int = 4 << 20, now: float | None = None) -> int:
        """Read & parse new complete lines. Returns bytes consumed."""
        if not self.tail.stat():
            return 0
        start = self.tail.offset
        while self.tail.pending and self.tail.offset - start < budget_bytes:
            lines = self.tail.read_lines(min(4 << 20, budget_bytes))
            if not lines:
                break
            for line in lines:
                self.feed_line(line)
        return self.tail.offset - start

    def feed_line(self, line: bytes) -> None:
        if not line:
            return
        try:
            r = json.loads(line)
        except Exception:
            return
        if isinstance(r, dict):
            try:
                self.feed(r)
            except Exception:
                pass

    # ------------------------------------------------------------------ helpers
    def _emit(self, ts: float, kind: str, **kw) -> None:
        if ts and ts >= self.emit_after:
            ev = {k: v for k, v in kw.items() if v not in (None, "")}
            ev["ts"] = int(ts)
            self.events.append((kind, ev))

    def _day_bucket(self, ts: float):
        if not ts:
            return None
        d = time.strftime("%Y-%m-%d", time.localtime(ts))
        today = time.strftime("%Y-%m-%d")
        if d != today:
            return None
        if self.day != today:
            self.day = today
            self.today = [0, 0.0, 0, 0]
        return self.today

    def todos_summary(self) -> dict | None:
        if not self.todos:
            return None
        done = sum(1 for t in self.todos if t.get("status") == "completed")
        cur = next((t for t in self.todos if t.get("status") == "in_progress"), None)
        if cur is None:
            cur = next((t for t in self.todos if t.get("status") != "completed"), None)
        txt = (cur.get("content") or "") if cur else ""
        return {"done": done, "total": len(self.todos), "current": ascii_text(txt, 60)}

    def ctx_pct(self) -> int:
        if not self.ctx_tokens:
            return 0
        return max(0, min(100, int(round(100.0 * self.ctx_tokens / context_window(self.ctx_model or self.model)))))

    def pending_ask(self):
        """(canon_tool, detail) of a pending ask_question call, else None."""
        for canon, _raw, detail, _ts, _meta in reversed(self.pending):
            if canon in ASK_TOOLS:
                return canon, detail
        return None

    def last_pending(self):
        if not self.pending:
            return None
        canon, _raw, detail, ts, _meta = self.pending[-1]
        return canon, detail, ts

    def flush_stop(self, now: float | None = None, grace: float = 0.0) -> None:
        sp = self.stop_pending
        if not sp or (now is not None and now - sp[2] < grace):
            return
        self.stop_pending = None
        if not self.is_sub:
            self._emit(sp[1], "stop", text=ascii_text(plain(_first_line(self.turn_text)), 80))

    # ------------------------------------------------------------------ record handling
    def feed(self, r: dict) -> None:
        step = r.get("step_index")
        if isinstance(step, int):
            if step in self._seen_steps:
                return
            self._seen_steps.add(step)
            if len(self._seen_steps) > 4096:
                self._seen_steps = set(sorted(self._seen_steps)[-1024:])
        ts = parse_ts(r.get("created_at") or r.get("timestamp"))
        if ts:
            if not self.first_ts:
                self.first_ts = ts
            if ts > self.last_ts:
                self.last_ts = ts
        rtype = r.get("type") or ""
        if rtype == "USER_INPUT":
            self._user_input(r, ts)
        elif rtype == "PLANNER_RESPONSE":
            self._planner_response(r, ts)
        elif rtype == "GENERIC":
            self._generic(r, ts)

    def _user_input(self, r: dict, ts: float) -> None:
        if self.stop_pending:
            self.flush_stop()
        content = r.get("content") or ""
        if not self.cwd:
            m = _CWD_HINT.search(content)
            if m:
                self.cwd = m.group(1).strip()
                self.cwd_inferred = True
        src = r.get("source") or ""
        if src and src != "USER_EXPLICIT":
            return
        prompt = extract_user_prompt(content)
        if not prompt:
            return
        if self.is_sub:
            if not self.last_prompt:
                self.last_prompt = ascii_text(prompt, 120)
            self.turn_open = True
            self.last_activity_ts = max(self.last_activity_ts, ts)
            return
        self.last_prompt = ascii_text(prompt, 120)
        self.last_prompt_ts = ts
        self.turn_text = ""
        self.pending.clear()
        self.last_activity_ts = max(self.last_activity_ts, ts)
        self.turn_open = True
        self.activity = {"tool": "thinking", "detail": "", "ts": int(ts)}
        bk = self._day_bucket(ts)
        if bk is not None:
            bk[3] += 1
        self._emit(ts, "prompt", text=self.last_prompt)

    def _planner_response(self, r: dict, ts: float) -> None:
        if self.stop_pending:
            self.flush_stop()
        model = r.get("model") or ""
        if model and not model.startswith("<"):
            self.model = model
        self.last_activity_ts = max(self.last_activity_ts, ts)
        self.turn_open = True

        i = int(r.get("input_tokens") or 0)
        o = int(r.get("output_tokens") or 0)
        cr = int(r.get("cache_read_tokens") or 0)
        if i or o or cr:
            self.ctx_tokens = i + cr
            if model:
                self.ctx_model = model
            cost = usage_cost(model or self.model, i, o, cr, 0)
            self.tokens[0] += i
            self.tokens[1] += o
            self.tokens[2] += cr
            self.cost += cost
            bk = self._day_bucket(ts)
            if bk is not None:
                bk[0] += i + o
                bk[1] += cost

        txt = (r.get("content") or "").strip()
        if txt:
            self.last_text = txt
            self.turn_text = txt
        tool_calls = r.get("tool_calls") or []
        if isinstance(tool_calls, list) and tool_calls:
            for tc in tool_calls:
                if isinstance(tc, dict):
                    self._tool_call(tc, ts)
            return

        # No tool calls: either pure thinking or end of turn
        if txt:
            self.activity = {"tool": "writing", "detail": ascii_text(plain(_first_line(txt)), 80), "ts": int(ts)}
            self.turn_open = False
            self.turn_end_ts = ts
            self.pending.clear()
            self.stop_pending = (r.get("step_index"), ts, time.time())
        elif r.get("thinking"):
            self.activity = {"tool": "thinking", "detail": "", "ts": int(ts)}

    def _tool_call(self, tc: dict, ts: float) -> None:
        raw = tc.get("name") or ""
        args = decode_agy_args(tc.get("args"))
        if not self.cwd:
            for k in ("Path", "TargetFile", "DirectoryPath", "SearchPath", "Cwd"):
                v = args.get(k)
                if isinstance(v, str) and v:
                    guess = _infer_project_root(v)
                    if guess:
                        self.cwd = guess
                        self.cwd_inferred = True
                        break
        canon = normalize_tool(raw)
        detail = tool_detail(canon, args, self.cwd)
        meta = {}
        if raw == "invoke_subagent":
            meta = {"role": args.get("Role") or "", "type": args.get("TypeName") or ""}
        self.pending.append((canon, raw, detail, ts, meta))
        if len(self.pending) > 64:
            self.pending.popleft()
        self.tool_count += 1
        bk = self._day_bucket(ts)
        if bk is not None:
            bk[2] += 1
        lbl = tool_label(canon)
        self.activity = {"tool": lbl, "detail": detail, "ts": int(ts)}
        ev = {"tool": lbl, "text": detail}
        if self.is_sub:
            ev["agent"] = (self.agent_id or "")[:8]
        self._emit(ts, "tool", **ev)
        if raw == "update_steps":
            self._update_steps(args, ts)

    def _update_steps(self, args: dict, ts: float) -> None:
        steps = args.get("steps")
        if not isinstance(steps, list):
            return
        old = {t.get("content"): t.get("status") for t in self.todos}
        new = []
        for s in steps:
            if not isinstance(s, dict):
                continue
            st = str(s.get("status") or "pending")
            if st == "done":
                st = "completed"
            new.append({"content": str(s.get("title") or s.get("content") or ""), "status": st})
        for t in new:
            if t["status"] == "completed" and old.get(t["content"]) not in (None, "completed"):
                self._emit(ts, "todo_done", text=ascii_text(t["content"], 80))
        self.todos = new

    def _generic(self, r: dict, ts: float) -> None:
        self.last_activity_ts = max(self.last_activity_ts, ts)
        canon, raw, detail, _t0, meta = self.pending.popleft() if self.pending else ("", "", "", 0.0, {})
        content = r.get("content") or ""
        if raw == "invoke_subagent":
            for cid in _UUID.findall(content):
                if cid != self.sid:
                    self.child_agents.setdefault(cid, {
                        "role": meta.get("role") or "",
                        "type": meta.get("type") or "",
                        "started": ts or time.time(),
                    })
                    break
        # Detect command/tool errors
        err_msg = ""
        m = _EXIT_ERR.search(content)
        if m:
            err_msg = f"exit code {m.group(1)}"
        elif r.get("status") == "ERROR":
            err_msg = _first_line(content) or "error"
        if err_msg and canon:
            ev = {"tool": tool_label(canon), "text": detail or ascii_text(err_msg, 80), "error": ascii_text(err_msg, 80)}
            if self.is_sub:
                ev["agent"] = (self.agent_id or "")[:8]
            self._emit(ts, "tool_error", **ev)
