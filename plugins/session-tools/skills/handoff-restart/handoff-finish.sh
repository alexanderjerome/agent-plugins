#!/usr/bin/env bash
# The last step of a handoff: start the new session, archive this one, stop this one.
#
#   handoff-finish.sh <handoff file, relative to the repo root> [new session name]
#
# The name defaults to <date>_<owner/repo> (`crc name`; the path from ~ outside a repository).
#
# Run from the repository root, from inside the session being handed off (it reads
# CLAUDE_CODE_SESSION_ID and CLAUDE_PID). Prints where the new session and the archive are.
# Stopping happens in a detached unit a few seconds later, so this script's output still reaches
# the session before it ends. It stops this session only once the new one has a Remote Control
# link (printed as "NEW SESSION LINK: https://claude.ai/code/session_..."); the session posts it to
# the user in the 30 s before it stops.
set -euo pipefail

handoff="${1:?handoff file}"
sid="${CLAUDE_CODE_SESSION_ID:?not inside a Claude Code session}"
pid="${CLAUDE_PID:-}"
config_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
enc="$(printf '%s' "$root" | sed 's/[^A-Za-z0-9]/-/g')"
name="${2:-$(crc name "$root" 2>/dev/null || echo "$(date +%F)_$(basename "$root")")}"

# Where this session runs: the cloud (claude.ai/code environment), under a `claude remote-control`
# server (claude-rc unit or rc parent), or a standalone terminal session.
where=standalone
if [ "${CLAUDE_CODE_REMOTE:-}" = true ]; then
  where=cloud
elif grep -q 'claude-rc-' "/proc/${pid:-self}/cgroup" 2>/dev/null \
  || { [ -n "$pid" ] && tr '\0' ' ' < "/proc/$(awk '{print $4}' "/proc/$pid/stat")/cmdline" 2>/dev/null | grep -q 'remote-control'; }; then
  where=rc
fi
echo "session kind: $where"

prompt="Read $handoff and continue the work it describes. Start with its First steps."
if [ "$where" = cloud ]; then
  # No local daemon or Remote Control there: the user starts the next session from claude.ai.
  echo "cloud session: start the next one from claude.ai/code with this prompt:"
  echo "  $prompt"
  exit 0
fi

# A live session already called $name (usually this one: the default name is per day and repo)
# would make the new one indistinguishable in the app; number it instead.
taken="$(claude agents --json 2>/dev/null | jq -r '.[].name // empty')"
if printf '%s\n' "$taken" | grep -qxF "$name"; then
  n=2
  while printf '%s\n' "$taken" | grep -qxF "$name-$n"; do n=$((n + 1)); done
  name="$name-$n"
fi

# 1. The new session, in the background, with Remote Control on so it shows in the Claude app.
cd "$root"
before="$(claude agents --json 2>/dev/null | jq -r '.[].id' | sort)"
claude --bg --remote-control -n "$name" "$prompt"

# Its link: the new agent's pid, then the Remote Control id its sessions/<pid>.json records once
# it has registered with claude.ai. Without a link this session is not stopped: the user would
# have nothing to open.
url=""
for _ in $(seq 60); do
  new="$(claude agents --json 2>/dev/null | jq -r '.[].id' | sort | comm -13 <(printf '%s\n' "$before") - | head -1)"
  if [ -n "$new" ]; then
    npid="$(claude agents --json | jq -r --arg i "$new" '.[] | select(.id==$i) | .pid')"
    bridge="$(jq -r '.bridgeSessionId // empty' "$config_dir/sessions/$npid.json" 2>/dev/null || true)"
    if [ -n "$bridge" ]; then url="https://claude.ai/code/$bridge"; break; fi
  fi
  sleep 2
done
if [ -z "$url" ]; then
  echo "new session ${new:-?} ($name) started, but no Remote Control link after 120 s;"
  echo "this session keeps running: check \`claude agents\` and \`claude logs ${new:-<id>}\`"
  exit 1
fi
echo "new session: $name (agent $new)"
echo "NEW SESSION LINK: $url"

# 2. Archive this session: transcript, its folder (subagents, tool results) and its task outputs.
arch="/tmp/claude/sessions/archived/$(date '+%Y-%m-%dT%H%M%S')_${sid}"
mkdir -p "$arch"
cp -a "$config_dir/projects/$enc/$sid.jsonl" "$arch/"
[ -d "$config_dir/projects/$enc/$sid" ] && cp -a "$config_dir/projects/$enc/$sid" "$arch/session-dir"
tasks="/tmp/claude-$(id -u)/$enc/$sid/tasks"
[ -d "$tasks" ] && cp -a "$tasks" "$arch/tasks"
echo "archived to $arch"

# 3. Stop this session once this script has returned. A background session is stopped through the
# daemon (its conversation stays resumable); a terminal or rc-spawned one gets SIGTERM.
short="$(claude agents --json 2>/dev/null | jq -r --arg s "$sid" '.[] | select(.sessionId==$s and .kind=="background") | .id' | head -1)"
if [ -n "$short" ]; then
  stop="claude stop $short"
elif [ -n "$pid" ]; then
  stop="kill -TERM $pid"
else
  echo "cannot tell how to stop this session; stop it by hand"; exit 0
fi
systemd-run --user --quiet --collect --unit "handoff-stop-${sid:0:8}" --on-active=30 \
  "$(command -v bash)" -lc "cp -a '$config_dir/projects/$enc/$sid.jsonl' '$arch/' ; $stop"
echo "this session stops in 30 s ($stop); the transcript is copied again just before"
echo "post the NEW SESSION LINK to the user now"
