---
name: handoff-restart
description: Use when the user says the conversation has run too long and wants to restart, or asks for a handoff into a new session. Ends this session's running tasks (asking which the next session should resume), writes a handoff document into ./.handoffs/<ISO date>-handoff.md in the current repository, starts a new named Claude Code session pointed at it, then archives and stops this one. Based on mattpocock's `handoff` skill, with the user's changes (2026-10-27).
argument-hint: "What will the next session be used for?"
---

# handoff-restart: hand the work to a fresh session

1. **Where is this session?** `echo "${CLAUDE_CODE_REMOTE:-} ${CLAUDE_CODE_BRIDGE_SESSION_ID:-} $CLAUDE_CODE_SESSION_ID"`.
   - `CLAUDE_CODE_REMOTE=true`: a claude.ai/code cloud environment. No daemon, no Remote Control,
     no local archive: write the document (steps 2-4), commit or push it if the environment would
     otherwise lose it, and give the user the prompt for the next session. Skip step 5.
   - Otherwise local: standalone in a terminal, or spawned by a `claude remote-control` server
     (a `claude-rc-*` unit; `crc envs` lists them). Both continue with all steps; the new session
     starts with Remote Control either way.

2. **Running work.** List every background shell, sub-agent, monitor and workflow of this session
   still running (the task list; outputs are in `/tmp/claude-<uid>/<encoded repo path>/<session
   id>/tasks/`). If there are any, ask the user with AskUserQuestion (multiSelect, one option per
   task: label = short description, description = what it is doing and its id) which ones the next
   session should resume. Then stop all of them (TaskStop; a workflow too). For each chosen one,
   note in the handoff what it was doing, the command or prompt to restart it, and its output file.
   The next session restarts them; claude does not carry tasks over by itself.

3. **Write the handoff document** to `<repo root>/.handoffs/<ISO date>-handoff.md` (add `-2`, `-3`
   if the name exists; create `.handoffs/`). Summarise the conversation so a fresh agent can go on:
   - what the user asked for and decided (quote decisions with dates), what is done (commits, PRs,
     deploys, with ids), what is in progress, what is blocked and on whom;
   - **"Tasks to resume"**: the ones the user chose in step 2, with how to restart each, and the
     tasks folder above;
   - **"Previous session"**: this session's id, its transcript
     (`$CLAUDE_CONFIG_DIR/projects/<encoded repo path>/<id>.jsonl`) and its archive folder
     (`/tmp/claude/sessions/archived/<date>_<id>/`, written in step 5);
   - **"Suggested skills"** the next agent should call; **"First steps"** for it.
   Reference ADRs, issues, commits and memory files by path, number or URL instead of repeating
   them. Redact secrets, keys, tokens and personal data. Tailor it to the user's arguments.

4. **Keep it out of git**: `.handoffs/` is local. If the repository does not ignore it, add
   `.handoffs/` to `.git/info/exclude` (not to a tracked .gitignore).

5. **Start the new session, archive and stop this one**, from the repository root:

   ```bash
   "${CLAUDE_SKILL_DIR}/handoff-finish.sh" .handoffs/<file> [name]
   ```

   The name is `<ISO date>_<owner/repo>` (or the path from ~ outside a repository; `crc name`
   prints it) unless the user gave one.

   It starts `claude --bg --remote-control -n <name>` on the handoff (numbering the name `-2`, `-3`
   when a live session already has it), waits until the new session has registered with Remote
   Control and prints `NEW SESSION LINK: https://claude.ai/code/session_...`, copies this session's
   transcript, its folder and its task outputs to `/tmp/claude/sessions/archived/<date>_<id>/`, and
   stops this session 30 seconds later (`claude stop` for a background session, SIGTERM
   otherwise). If no link appears within two minutes it exits 1 and stops nothing: tell the user
   and do not stop this session by hand.

6. **Post the link** at once, in the reply (and with PushNotification when it is available): the new
   session's name, its `NEW SESSION LINK` exactly as printed (the user opens it directly in the
   Claude app), and the archive folder. Nothing after that reply runs.
