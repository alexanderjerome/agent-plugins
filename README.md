# agent-plugins

A plugin marketplace for Claude Code and Codex: a few plugins of my own plus pinned references to upstream plugins I use. Upstream plugins are not copied here; each entry points at the upstream repository at a fixed commit.

## Set it up with an agent

Paste this into Claude Code or Codex, in any directory. It installs everything below and wires the TOON gateway; it asks before changing your agent configuration.

```text
Set up the agent-plugins marketplace (github.com/alexanderjerome/agent-plugins) for me. Read its
README first; it is the reference. Then:

1. Work out which agents I have: `claude --version`, `codex --version`. Do the steps below for each
   one found, and skip the other.
2. Prerequisites, installing what is missing with the package manager this machine already uses
   (ask before installing anything system-wide): `gh` (tstack), `node` (ponytail, caveman, adr
   scripts), the `codegraph` CLI (github.com/colbymchenry/codegraph, Claude only), and `mcptoon`
   (`pipx install mcptoon` or `uv tool install mcptoon`).
3. Claude Code: `claude plugin marketplace add alexanderjerome/agent-plugins`, then
   `claude plugin install <name>@agent-plugins` for tstack, adr, session-tools, codegraph, archify,
   ponytail, caveman and mcptoon.
4. Codex: `codex plugin marketplace add alexanderjerome/agent-plugins`, then
   `codex plugin add <name>@agent-plugins` for tstack, adr, ponytail and caveman. Codex has no
   mcptoon plugin: register the gateway with `codex mcp add mcptoon -- mcptoon serve`.
5. TOON gateway: run `mcptoon status` and show me what it found, then `mcptoon quickstart` to put my
   existing MCP servers behind the gateway. Tell me what it changed and that `mcptoon off` or
   `mcptoon restore` undoes it. Do not remove any MCP server yourself.
6. Check: `claude plugin list` / `codex plugin list` show the plugins enabled, and `mcptoon manifest`
   lists my servers' tools. Report what was installed, what was skipped and why, and anything that
   needs me (a login, a restart of the agent).
```

## Install

Claude Code:

```
/plugin marketplace add alexanderjerome/agent-plugins
/plugin install tstack@agent-plugins
```

Codex (CLI 0.167 or later):

```
codex plugin marketplace add alexanderjerome/agent-plugins
codex plugin add tstack@agent-plugins
```

`.claude-plugin/marketplace.json` is the Claude Code catalog, `.agents/plugins/marketplace.json` the Codex one.

| Plugin | Claude Code | Codex |
|---|---|---|
| tstack | yes | yes |
| adr | yes | yes |
| session-tools | yes | no (Claude-only: edits Claude config, uses Claude remote control) |
| codegraph | yes | no (the MCP server and a Claude `UserPromptSubmit` hook; no Codex manifest) |
| archify | yes | no (upstream ships no Codex plugin) |
| ponytail | yes | yes |
| caveman | yes | yes |
| mcptoon | yes | no (upstream ships no Codex plugin; see below) |

## Plugins

**tstack** (own, MIT, from Theo Browne's tstack): skills for shipping a change: `full-send`, `full-stop`, `sol-loop`, `council`, `babysit-pr` (with a `pr-wait` script), `file-pr`, `modernize`, `takeover`, `leaving-pr-comments`. Needs `gh`. See `plugins/tstack/LICENSE`.

**adr** (own): write, accept, reject or supersede Architecture Decision Records with templates and scripts (node). Vendored from [skillrecordings/adr-skill](https://github.com/skillrecordings/adr-skill) at 2fc2269, which carries no license file; remove this plugin if that matters to you.

**session-tools** (own, Claude Code only): `handoff-restart` writes a handoff document and starts a fresh session (uses `claude remote-control`, `systemd-run`, and an optional `crc` helper for naming); `claude-config-form` serves a one-shot browser form to review and edit CLAUDE.md and settings.json.

**codegraph** (own, Claude Code only): declares the CodeGraph MCP server and a hook that adds structural context to each prompt. Install the `codegraph` CLI separately.

**archify** (upstream [tt-a1i/archify](https://github.com/tt-a1i/archify), Apache-2.0, pinned a406594): turns an idea, plan or codebase into an interactive architecture, sequence or data-flow diagram.

**ponytail** (upstream [DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail), MIT, pinned 96608e0): lazy-senior-dev mode, the simplest code that works. Hooks need `node`.

**caveman** (upstream [JuliusBrussee/caveman](https://github.com/JuliusBrussee/caveman), Apache-2.0, pinned 9b24366): ultra-compressed replies. Claude Code uses the repository root as the plugin; Codex uses its `plugins/caveman` directory. Hooks need `node`.

**mcptoon** (upstream [activeing123/mcptoon](https://github.com/activeing123/mcptoon), Apache-2.0, pinned f50d199): the TOON gateway, which puts your other MCP servers behind a few compact tools. The Claude plugin declares the server and a setup command, but the binary is separate: `pip install mcptoon` (or your package manager), then `mcptoon quickstart`. Codex has no plugin from upstream; register the server yourself with `codex mcp add mcptoon -- mcptoon serve`.
