---
name: claude-config-form
description: Use when the user wants to edit Claude Code's own CLAUDE.md files or settings.json (permissions, hooks, instructions) from a browser. Starts a one-shot editor on the tailnet and LAN addresses; the user edits and submits, changed files are replaced with backups in /tmp.
---

# claude-config-form: let the user review and edit Claude's CLAUDE.md files and settings.json

Claude does not edit its own permissions or instructions; the user does, through this form.

1. If you have a proposal, write it to files first (full proposed CLAUDE.md files and/or settings.json;
   keep them out of the config dir), then start the form in the background (no monitor loop; you are
   told when it exits):

   ```bash
   python3 -u "${CLAUDE_SKILL_DIR}/config_form.py" --bind tailnet --bind lan \
     [--proposed-claude-md PATH] [--proposed-settings PATH] \
     [--project-claude-md <repo>/CLAUDE.md [--proposed-project-claude-md PATH]] \
     [--project-dir DIR] [--request-file PATH | --request TEXT] [--state-dir DIR]
   ```

   `--project-claude-md` adds the project CLAUDE.md editor and makes the project's folder the source of
   the project listings below. `--bind` also takes a literal address (e.g. `127.0.0.1`).

   `--project-dir` (default: the current directory) is what the page header shows: git top-level,
   origin as owner/repo and branch, or just the directory outside git. `--request-file` / `--request`
   fills the read-only "User request" block at the top (hidden without one). `--state-dir` (default
   `~/.cache/config-form/<UTC>`, printed as `STATE DIR:`) holds `feedback.jsonl` and
   `config-form.json` (state dir, proposal paths, pid).

2. Give the user all three labelled `FORM URL` lines, in the printed order: Tailscale DNS (the trusted name; needs "Use Tailscale DNS" on their device), Tailscale IP (no DNS needed; over HTTPS one name warning), LAN (home network).
   Port 8766 (`--port`) must be reachable on the bound interfaces.

Feedback loop: under the request the page has a feedback box. Submit appends the text (numbered,
timestamped) to `feedback.jsonl` and the page says "Sent to Claude; waiting for revised proposals".
Start the form and, next to it, in the background:

```bash
python3 -u "${CLAUDE_SKILL_DIR}/config_form.py" wait --state-dir DIR [--timeout S]
```

It blocks until there is feedback newer than the last it returned, prints it with the proposal file
paths and exits 0 (3: timeout; 4: the form has ended). When it exits, edit the proposal files as the
feedback asks, then run `wait` again. The form re-reads proposal files when their mtime changes and
the page polls every 4 seconds, re-rendering the highlighted sections; a Final editor the user
edited is kept and marked "a newer proposal is available". When the form exits (all editors
submitted, Done, or timeout), stop the `wait` too.

Page layout: a navigation of components on the left (on a phone, on top), each with a checkbox that
shows or hides it in the main view and a list of its sections. Components: the editors (User
CLAUDE.md, Project CLAUDE.md if given, settings.json; sections = markdown headings, JSON top-level
keys, with `permissions.*` and `hooks.<event>` split out) and read-only listings of MCP servers
(`~/.mcp.json`, the project's `.mcp.json`, `mcpServers` in settings; env and header values hidden),
skills, agents, dynamic workflows and commands (config dir and project `.claude/`; name and
description). Missing folders are empty.

In the main view every section is collapsed, except those the proposal changes: they are
highlighted (also in the navigation), open, and show the Proposed text with the Current text beside
it (changed lines marked). Clicking a title collapses or expands it; a navigation link opens and
scrolls to it.

Each editor also has the views Current (disk, read-only), Proposed (your file, read-only; "Use
proposed" is disabled without one) and Final (editable, starts as Current; "Use proposed" copies the
proposal into it). Each editor has its own Submit, sending only its Final text: unchanged -> "No
changes; nothing to do."; otherwise the file is backed up (`/tmp/claude-config-backup-<UTC>-<file>`,
`project-CLAUDE.md` for the project one) and replaced, and Current is refreshed. settings.json is
only written if it parses as JSON. The server stays up for the other editors and exits 0 when all
are replaced or on "Done", printing a summary of replaced files and backups. Exit 3: timeout (default 8 hours, `--timeout S`).

The files are `$CLAUDE_CONFIG_DIR/CLAUDE.md` and `$CLAUDE_CONFIG_DIR/settings.json` (default
`~/.claude`) and the given project CLAUDE.md. Settings changes take effect in new sessions
(or after `/hooks`).
