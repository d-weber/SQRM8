# Multi-Agent Support (Claude Code, Antigravity, OpenCode)

SQRM8's wire protocol (`PROTOCOL.md`) and ESP32-S3 firmware are agent-agnostic: every screen renders normalized
`state.sessions`, `state.totals`, `state.usage`, `state.system`, and `event` messages. All agent-specific
discovery, hook translation, and tool/model normalization live in the Python host daemon (`host/companion/`),
so multiple agents run side-by-side without reflashing the device.

## Enabled Agents

By default, SQRM8 runs all supported session providers simultaneously and aggregates their sessions, totals,
usage metrics, and events. To restrict which agents run, set `"agents"` in `~/.sqrm8/settings.json`:

```json
{"agents": ["claude", "antigravity"]}
```

## Supported Agents

### Claude Code (`source: "claude"`)
- **Sessions & transcripts:** `companion/providers/sessions.py` reads `~/.claude/sessions/<pid>.json` and tails
  `~/.claude/projects/<slug>/<sid>.jsonl` (+ subagent transcripts).
- **Real-time hooks:** `companion/hooksetup.py` merges async/sync command hooks + `statusLine` wrapper into
  `~/.claude/settings.json`.
- **Plan limits:** statusLine tee (`~/.sqrm8/statusline.json`), optional Anthropic OAuth usage endpoint, and
  `~/.claude.json` fallback.

### Antigravity (`source: "antigravity"`)
Covers all three Antigravity surfaces automatically (prioritizing CLI `agy`):
- **CLI (`agy`):** `~/.gemini/antigravity-cli/brain/<cid>/.system_generated/logs/transcript.jsonl`
- **Antigravity 2.0 App:** `~/.gemini/antigravity/brain/<cid>/.system_generated/logs/transcript.jsonl`
- **Antigravity IDE:** `~/.gemini/antigravity-ide/brain/<cid>/.system_generated/logs/transcript.jsonl`

Integration pieces:
- **Transcript provider (`companion/providers/antigravity.py` + `companion/antigravity_transcript.py`):**
  incrementally tails `transcript.jsonl`, normalizes tool names (`run_command` → `Bash`, `read_file` → `Read`,
  `edit_file` → `Edit`, `update_steps` → `TodoWrite`, `ask_question` → `AskUserQuestion`, etc.), extracts todos,
  waiting questions, subagents, tokens, context %, and estimated API cost.
- **Global hooks (`~/.gemini/config/hooks.json`, key `"sqrm8"`):**
  `sqrm8 hooks install --agent antigravity` (or `--agent auto`) registers SQRM8 on the 5 safe observational
  lifecycle events: `SessionStart`, `PreInvocation`, `PostToolUse`, `PostInvocation`, and `Stop`.
  - **`PreToolUse` is intentionally omitted:** Antigravity evaluates `PreToolUse` as a synchronous, fail-closed
    permission gate (`{}` blocks tools; `"allow"` would bypass user permission prompts).
  - `sqrm8 hook --agent antigravity` translates camelCase payloads to SQRM8 datagrams and always writes `{}` to
    stdout so Antigravity never fails a hook even when the daemon is stopped.
- **Usage aggregation:** `companion/providers/usage.py` reads `PLANNER_RESPONSE` token counts from Antigravity
  transcripts into the shared `tpm`/`hourly`/`daily`/`today`/`week`/`by_model` stats.

---

## Adding OpenCode (Drop-in Blueprint)

The multi-agent plumbing is already prepared for OpenCode (`sessions_text.py` already maps OpenCode tool names
`read`, `edit`, `patch`, `write`, `bash`, `grep`, `glob`, `ls`, `webfetch`, `websearch`, `task`, `todowrite`,
`question` and `hostos.opencode_data_dir()` resolves `~/.local/share/opencode` / `%LOCALAPPDATA%\opencode`).

To add OpenCode support without changing daemon core logic:

1. **Session & Usage Reader (`companion/providers/opencode.py`):**
   - Open `<opencode_data_dir>/opencode.db` with stdlib `sqlite3` in read-only URI mode
     (`sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.1)`).
   - Poll `session` (`id`, `parent_id`, `directory`, `title`, `time_created`, `time_updated`), `message`
     (`data` JSON: `role`, `modelID`, `tokens`, `cost`, `time`), and `part` (`data` JSON: `type`, `text`,
     `tool`, `state`) incrementally by `time_updated`.
   - Map rows with `parent_id` into their parent session's `subagents[]`, emit `"source": "opencode"`, and
     implement `Provider.focus_info(sid)` and `Provider.hook_covered_kinds()`.
   - Register `OpenCodeProvider` in `daemon.build_providers()` and add an OpenCode SQLite reader to
     `UsageProvider`.

2. **Real-Time Plugin (`host/hooks/sqrm8_opencode.js` → `~/.config/opencode/plugins/sqrm8.js`):**
   - Export an OpenCode plugin subscribing to `event` (`session.status`, `session.idle`, `permission.asked`,
     `permission.replied`, `todo.updated`, `session.compacted`) and `tool.execute.before` / `tool.execute.after`.
   - Pipe normalized JSON to `sqrm8 hook --agent opencode` (the `--agent opencode` flag is already recognized by
     `companion/hookclient.py` and tags datagrams with `"sa": "opencode"`).
   - Add `install_opencode()` / `uninstall_opencode()` / `opencode_check()` to `companion/hooksetup.py` and add
     `(str(p), "hooks")` for `*.js` in `packaging/sqrm8.spec`.
