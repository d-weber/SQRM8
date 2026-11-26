"""Antigravity sessions provider (CLI `agy`, Antigravity 2.0 App, and Antigravity IDE).

Turns Antigravity's local `transcript.jsonl` files into normalized `state.sessions`, `state.totals`,
and `event`s (PROTOCOL.md). Read-only: never writes anything under ~/.gemini.

Sources
-------
~/.gemini/antigravity-cli/brain/<cid>/.system_generated/logs/transcript.jsonl   (Antigravity CLI)
~/.gemini/antigravity/brain/<cid>/.system_generated/logs/transcript.jsonl       (Antigravity 2.0 App)
~/.gemini/antigravity-ide/brain/<cid>/.system_generated/logs/transcript.jsonl   (Antigravity IDE)

Real-time status/cwd/prompt updates arrive via Antigravity global hooks (`~/.gemini/config/hooks.json`,
merged by daemon.merge_hooks); this provider supplies live tool calls, `ask_question` waiting prompts,
`update_steps` todos, subagent linking, tokens, cost, and context %.
"""
from __future__ import annotations

import glob
import logging
import os
import time

from .base import Provider
from .. import hostos
from ..antigravity_transcript import AntigravityTranscript
from ..sessions_text import ascii_text, folder_name, model_label, tilde

log = logging.getLogger("companion.antigravity")

IDLE_ALIVE_S = 900          # transcript quiet for this long -> offline
OFFLINE_KEEP_S = 600        # keep offline sessions this long before dropping
SUB_DONE_KEEP_S = 300       # keep finished subagents in the list this long
SUB_STALE_S = 1800          # silent subagent considered done
SUB_MAX = 12
SCAN_S = 3.0
REST_POLL_S = 2.0
POLL_BUDGET_S = 0.12
EVENT_MAX_AGE_S = 90

# Antigravity hooks omit PreToolUse (fail-closed permission gate), so hooks only cover these kinds.
# Transcript-emitted `tool`, `waiting`, `todo_done`, `subagent_start/stop` remain active!
AGY_HOOK_KINDS = {"session_start", "prompt", "stop", "tool_error"}


class _AgySub:
    __slots__ = ("aid", "tr", "role", "agent_type", "announced", "done_ts", "stopped")

    def __init__(self, aid: str, tr: AntigravityTranscript, role: str = "", agent_type: str = ""):
        self.aid = aid
        self.tr = tr
        self.role = role
        self.agent_type = agent_type
        self.announced = False
        self.done_ts = 0.0
        self.stopped = False


class _AgySession:
    def __init__(self, cid: str, surface: str, tr: AntigravityTranscript):
        self.sid = cid
        self.short = cid[:8]
        self.surface = surface
        self.tr = tr
        self.cwd = ""
        self.started = 0.0
        self.status = ""
        self.since = 0.0
        self.ended = 0.0
        self.waiting = ""
        self.waiting_emit = False
        self.subs: dict[str, _AgySub] = {}

    @property
    def name(self) -> str:
        return ascii_text(folder_name(self.cwd) or self.short, 32)


class AntigravityProvider(Provider):
    source_name = "antigravity"

    def __init__(self, dirs: list[tuple[str, str]] | None = None):
        self.dirs = dirs if dirs is not None else hostos.antigravity_dirs()
        self.start = time.time()
        self.primed = False
        self.sessions: dict[str, _AgySession] = {}
        self.transcripts: dict[str, AntigravityTranscript] = {}   # path -> transcript
        self.cid_paths: dict[str, tuple[str, str]] = {}          # cid -> (surface, path)
        self.child_cids: set[str] = set()                        # cids owned as subagents
        self.last_scan = 0.0
        self.rest_poll_at = 0.0
        self.events: list[dict] = []
        self.poll_ms = 0.0

    # ================================================================== provider API
    def hook_covered_kinds(self) -> set[str]:
        return set(AGY_HOOK_KINDS)

    def poll(self, now: float) -> None:
        t0 = time.monotonic()
        if now - self.last_scan >= SCAN_S:
            self._scan(now)
        self._read_transcripts(now, t0)
        self._update(now)
        self.primed = True
        self.poll_ms = (time.monotonic() - t0) * 1000

    def drain_events(self) -> list[dict]:
        ev, self.events = self.events, []
        return ev

    def today_totals(self) -> tuple[int, float, int, int]:
        today = time.strftime("%Y-%m-%d")
        tok = tools = prompts = 0
        cost = 0.0
        for tr in self.transcripts.values():
            if tr.day == today:
                tok += tr.today[0]
                cost += tr.today[1]
                tools += tr.today[2]
                prompts += tr.today[3]
        return int(tok), cost, int(tools), int(prompts)

    def contribute_sessions(self) -> list[dict]:
        now = time.time()
        return [self._session_json(s, now) for s in self.sessions.values()]

    def focus_info(self, sid: str) -> dict | None:
        if not sid:
            return None
        for full_id, s in self.sessions.items():
            if full_id.startswith(sid):
                return {"sid": full_id, "cwd": s.cwd, "source": "antigravity", "surface": s.surface, "pid": None}
        return None

    def contribute(self, state: dict) -> None:
        out = self.contribute_sessions()
        existing = state.get("sessions")
        if isinstance(existing, list):
            seen = {str(x.get("id", ""))[:8] for x in existing if isinstance(x, dict)}
            merged = list(existing) + [x for x in out if x["id"] not in seen]
        else:
            merged = out

        def recency(x):
            return max(x.get("since") or 0, (x.get("activity") or {}).get("ts") or 0)

        merged.sort(key=lambda x: (x.get("status") == "offline", -recency(x)))
        state["sessions"] = merged

        tok, cost, tools, prompts = self.today_totals()
        totals = state.get("totals")
        if isinstance(totals, dict):
            totals["tokens_today"] = int(totals.get("tokens_today") or 0) + tok
            totals["cost_today"] = round(float(totals.get("cost_today") or 0.0) + cost, 2)
            totals["tools_today"] = int(totals.get("tools_today") or 0) + tools
            totals["prompts_today"] = int(totals.get("prompts_today") or 0) + prompts
        else:
            totals = state["totals"] = {
                "tokens_today": tok, "cost_today": round(cost, 2),
                "tools_today": tools, "prompts_today": prompts,
            }
        st = [x.get("status") for x in merged if isinstance(x, dict)]
        totals["sessions"] = sum(1 for x in st if x != "offline")
        for k in ("busy", "waiting", "idle", "offline"):
            totals[k] = st.count(k)
        totals["subagents_active"] = sum(
            1 for x in merged if isinstance(x, dict)
            for a in (x.get("subagents") or []) if isinstance(a, dict) and a.get("status") == "busy"
        )

    # ================================================================== discovery
    def _get_tr(self, path: str, cid: str, is_sub: bool) -> AntigravityTranscript:
        tr = self.transcripts.get(path)
        if tr is None:
            tr = self.transcripts[path] = AntigravityTranscript(path, cid, is_sub=is_sub, emit_after=self.start - 1.0)
        elif is_sub and not tr.is_sub:
            tr.is_sub = True
            tr.agent_id = cid
        return tr

    def _scan(self, now: float) -> None:
        self.last_scan = now
        lt = time.localtime(now)
        midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        keep_after = min(midnight, now - IDLE_ALIVE_S - OFFLINE_KEEP_S)
        for surface, base in self.dirs:
            brain = os.path.join(base, "brain")
            if not os.path.isdir(brain):
                continue
            pattern = os.path.join(glob.escape(brain), "*", ".system_generated", "logs", "transcript.jsonl")
            for p in glob.glob(pattern):
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                # .../<brain>/<cid>/.system_generated/logs/transcript.jsonl
                cid = os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(p))))
                if not cid:
                    continue
                self.cid_paths[cid] = (surface, p)
                if p not in self.transcripts and st.st_mtime >= keep_after:
                    self._get_tr(p, cid, cid in self.child_cids)
        live_paths = {s.tr.path for s in self.sessions.values()}
        live_paths |= {sub.tr.path for s in self.sessions.values() for sub in s.subs.values()}
        for p, tr in list(self.transcripts.items()):
            if p in live_paths:
                continue
            if not os.path.exists(p) or (tr.tail.mtime and tr.tail.mtime < keep_after - 3600 and not tr.tail.pending):
                del self.transcripts[p]

    def _read_transcripts(self, now: float, t0: float) -> None:
        order: list[AntigravityTranscript] = []
        for s in self.sessions.values():
            order.append(s.tr)
            order.extend(sub.tr for sub in s.subs.values())
        seen = {id(t) for t in order}
        rest = [t for t in self.transcripts.values() if id(t) not in seen]
        if now - self.rest_poll_at >= REST_POLL_S or any(t.tail.pending for t in rest):
            self.rest_poll_at = now
            order.extend(rest)
        else:
            order.extend(t for t in rest if t.tail.mtime == 0)
        deadline = t0 + POLL_BUDGET_S
        for tr in order:
            while time.monotonic() < deadline:
                if not tr.poll(budget_bytes=1 << 20) or not tr.tail.pending:
                    break
            if time.monotonic() >= deadline:
                break

    # ================================================================== state
    def _update(self, now: float) -> None:
        # Link child subagents discovered in any transcript first so they don't appear as top-level sessions
        for tr in list(self.transcripts.values()):
            for child_cid in tr.child_agents:
                self.child_cids.add(child_cid)
                if child_cid in self.sessions:
                    del self.sessions[child_cid]

        # Promote active / recent transcripts into tracked sessions
        for p, tr in list(self.transcripts.items()):
            if tr.is_sub or tr.sid in self.child_cids or tr.sid in self.sessions:
                continue
            last = max(tr.last_ts, tr.tail.mtime)
            if not last:
                continue
            if now - last <= IDLE_ALIVE_S or (tr.last_ts > self.start and now - tr.tail.mtime < IDLE_ALIVE_S):
                surface = self.cid_paths.get(tr.sid, ("cli", p))[0]
                s = self.sessions[tr.sid] = _AgySession(tr.sid, surface, tr)
                s.cwd = tr.cwd
                s.started = tr.first_ts or now
                if self.primed and tr.last_ts >= self.start - 2:
                    self._event("session_start", s, now, text=surface)

        for sid, s in list(self.sessions.items()):
            tr = s.tr
            if tr.cwd and (not s.cwd or tr.cwd_inferred is False):
                s.cwd = tr.cwd
            if not s.started and tr.first_ts:
                s.started = tr.first_ts
            tr.flush_stop(now, grace=1.5)
            for kind, ev in tr.events:
                self._event(kind, s, ev.pop("ts", now), **ev)
            tr.events.clear()

            # Attach newly discovered child subagents
            for child_cid, meta in tr.child_agents.items():
                if child_cid in s.subs:
                    continue
                loc = self.cid_paths.get(child_cid)
                if loc:
                    sub_tr = self._get_tr(loc[1], child_cid, True)
                    s.subs[child_cid] = _AgySub(child_cid, sub_tr, meta.get("role") or "", meta.get("type") or "")

            last = max(tr.last_ts, tr.tail.mtime)
            if s.status != "offline" and last and now - last > IDLE_ALIVE_S:
                s.status, s.since, s.ended, s.waiting = "offline", now, now, ""
                self._event("session_end", s, now)
            if s.status == "offline":
                self._update_subs(s, now)
                if now - s.ended > OFFLINE_KEEP_S:
                    del self.sessions[sid]
                continue
            self._update_subs(s, now)
            self._update_status(s, now)

        for tr in self.transcripts.values():
            if tr.events:
                tr.events.clear()

    def _update_status(self, s: _AgySession, now: float) -> None:
        tr = s.tr
        ask = tr.pending_ask()
        if ask:
            status = "waiting"
        elif tr.turn_open and (tr.pending or now - max(tr.last_ts, tr.tail.mtime) < 120):
            status = "busy"
        elif any(not sub.done_ts and sub.tr.first_ts for sub in s.subs.values()):
            status = "busy"
        else:
            status = "idle"
        if status != s.status:
            prev = s.status
            s.status = status
            s.since = tr.last_ts or now
            s.waiting = ""
            s.waiting_emit = bool(status == "waiting" and self.primed and (prev or tr.last_ts >= self.start - 2))
        if status == "waiting" and ask:
            text = ascii_text("Question: " + (ask[1] or "Needs your input"), 100)
            s.waiting = text
            if s.waiting_emit:
                s.waiting_emit = False
                self._event("waiting", s, s.since, text=text)

    def _update_subs(self, s: _AgySession, now: float) -> None:
        for aid, sub in list(s.subs.items()):
            tr = sub.tr
            if not tr.first_ts:
                continue
            if tr.events:
                for kind, ev in tr.events:
                    if kind in ("tool", "tool_error"):
                        self._event(kind, s, ev.pop("ts", now), **ev)
                tr.events.clear()
            desc = ascii_text(sub.role or tr.last_prompt or sub.agent_type or "subagent", 60)
            if not sub.announced:
                sub.announced = True
                if tr.first_ts >= self.start - 2 and self.primed:
                    self._event("subagent_start", s, tr.first_ts, agent=aid[:8], text=desc)
            done = 0.0
            if not tr.turn_open and tr.turn_end_ts:
                done = tr.turn_end_ts
            last = max(tr.last_ts, tr.tail.mtime)
            if not done and (now - last > SUB_STALE_S or s.status == "offline"):
                done = last
            if done and tr.last_ts > done + 2 and tr.turn_open:
                done = 0.0
            if done and not sub.done_ts:
                sub.done_ts = done
                if not sub.stopped and sub.announced and sub.done_ts >= self.start and self.primed:
                    self._event("subagent_stop", s, now, agent=aid[:8], text=desc)
                sub.stopped = True
            elif not done and sub.done_ts:
                sub.done_ts = 0.0
                sub.stopped = False

    # ================================================================== output
    def _event(self, kind: str, s: _AgySession, ts: float, **kw) -> None:
        now = time.time()
        if kind not in ("session_start", "session_end") and ts < now - EVENT_MAX_AGE_S:
            return
        e = {"t": "event", "kind": kind, "sid": s.short, "name": s.name, "ts": int(ts)}
        for k, v in kw.items():
            if v is not None and v != "":
                e[k] = ascii_text(v, 120) if isinstance(v, str) else v
        self.events.append(e)

    def _session_json(self, s: _AgySession, now: float) -> dict:
        tr = s.tr
        tok = list(tr.tokens)
        cost = tr.cost
        tools = tr.tool_count
        subs_out = []
        for aid, sub in s.subs.items():
            st = sub.tr
            for k in range(4):
                tok[k] += st.tokens[k]
            cost += st.cost
            tools += st.tool_count
            if not st.first_ts:
                continue
            if sub.done_ts and now - sub.done_ts > SUB_DONE_KEEP_S:
                continue
            subs_out.append({
                "id": aid[:8],
                "desc": ascii_text(sub.role or st.last_prompt or sub.agent_type or "subagent", 60),
                "type": ascii_text(sub.agent_type, 24),
                "status": "done" if sub.done_ts else "busy",
                "tool": st.activity.get("tool", ""), "detail": st.activity.get("detail", ""),
                "started": int(st.first_ts),
                "tokens": int(st.tokens[0] + st.tokens[1] + st.tokens[3]),
                "cost": round(st.cost, 2), "model": model_label(st.model), "ctx_pct": st.ctx_pct(),
            })
        subs_out.sort(key=lambda x: (x["status"] != "busy", -x["started"]))
        subs_out = subs_out[:SUB_MAX]
        activity = dict(tr.activity)
        busy_subs = [x for x in subs_out if x["status"] == "busy"]
        if s.status == "busy" and busy_subs and not tr.turn_open:
            n = len(busy_subs)
            ts = max([int(s.subs[a].tr.activity.get("ts") or 0) for a in s.subs
                      if a[:8] in {x["id"] for x in busy_subs}] + [activity.get("ts") or 0])
            activity = {"tool": "Agent", "detail": f"{n} subagent{'s' if n > 1 else ''} working", "ts": ts}
        project = ascii_text(folder_name(s.cwd), 32)
        out = {
            "id": s.short, "name": s.name, "project": project or s.short, "cwd": ascii_text(tilde(s.cwd), 80),
            "title": ascii_text(tr.title, 60),
            "status": s.status or "idle", "since": int(s.since or now),
            "started": int(s.started or tr.first_ts or now),
            "model": model_label(tr.model),
            "activity": activity,
            "last_prompt": tr.last_prompt,
            "tokens": {"in": tok[0], "out": tok[1], "cache_read": tok[2], "cache_write": tok[3]},
            "cost": round(cost, 2),
            "ctx_pct": tr.ctx_pct(),
            "tool_count": tools,
            "subagents": subs_out,
            "kind": ascii_text(s.surface, 16),
            "source": "antigravity",
        }
        if not out["activity"].get("ts"):
            out["activity"]["ts"] = int(s.started or now)
        td = tr.todos_summary()
        if td:
            out["todos"] = td
        if s.status == "waiting" and s.waiting:
            out["waiting"] = s.waiting
        return out
