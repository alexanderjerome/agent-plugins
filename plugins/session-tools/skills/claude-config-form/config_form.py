#!/usr/bin/env python3
"""One-shot browser editor for Claude Code's own CLAUDE.md files and settings.json.

    config_form.py [--bind tailnet --bind lan] [--port 8766] [--timeout 28800]
                   [--proposed-claude-md PATH] [--proposed-settings PATH]
                   [--project-claude-md PATH [--proposed-project-claude-md PATH]]
                   [--project-dir DIR] [--request-file PATH | --request TEXT] [--state-dir DIR]
    config_form.py wait --state-dir DIR [--timeout S]

Editors: the user CLAUDE.md and settings.json in $CLAUDE_CONFIG_DIR (default ~/.claude), and the
project CLAUDE.md when --project-claude-md is given. Read-only listings: MCP servers, skills,
agents, dynamic workflows and commands, from the config dir and from the project's folder (the
folder of --project-claude-md). The page has a navigation of components (markdown headings, JSON
top-level keys, listing entries), a show/hide toggle per component, and in the main view every
section collapsed unless the proposal changes it (those are highlighted and open, showing the
proposed text with the current text beside it).

Each editor has views Sections, Current (disk), Proposed (Claude's file) and Final (editable,
starts as Current), and submits on its own: unchanged Final -> "No changes; nothing to do.";
otherwise the file is copied to /tmp/claude-config-backup-<UTC time>-<name> and replaced
atomically. settings.json must parse as JSON or nothing is written. Prints `FORM URL:` lines (one
shared one-time token). The server stays up after a submission and exits when every editor's file
was replaced, on "Done", or on --timeout; on exit it prints what was replaced and the backups.
The page starts with a header (the git top-level, origin as owner/repo and branch of --project-dir,
default the current directory; outside git just the directory), the read-only "User request" (from
--request-file / --request) and a feedback box. Feedback is appended, numbered and timestamped, to
<state-dir>/feedback.jsonl (default ~/.cache/config-form/<UTC>); the state
dir also holds config-form.json (state dir, proposal paths, pid). `wait` blocks until there is
feedback newer than the last it returned, prints it and the proposal paths, exit 0 (3: timeout; 4:
the form has ended). The form re-reads the proposal files when their mtime changes and the page
polls /state every few seconds, so edited proposals appear in the same page.
Plain HTTP: tailnet (WireGuard) or the home LAN only, never all interfaces.
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import re
import secrets
import select
import subprocess
import sys
import time

DEFAULT_TIMEOUT = 8 * 3600
STATE_ROOT = os.path.expanduser("~/.cache/config-form")
STATE_FILE = "config-form.json"
FEEDBACK_FILE = "feedback.jsonl"

MAX_BODY = 4 * 1024 * 1024


def tailnet_address() -> str:
    out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True).stdout.split()
    if out:
        return out[0]
    raise SystemExit("--bind tailnet: this machine has no tailnet address")


def tailnet_name() -> str | None:
    """This machine's MagicDNS name, or None (no tailnet, or MagicDNS off)."""
    try:
        out = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=10).stdout
        return (json.loads(out).get("Self") or {}).get("DNSName", "").rstrip(".") or None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def link_lines(scheme: str, addresses: list[str], port: int, token: str, name: str | None) -> list[str]:
    """The links to hand the user, labelled, in order: the Tailscale DNS
    name, the Tailscale IP, the LAN address."""
    try:
        ts_ip = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=10).stdout.split()[:1]
    except (OSError, subprocess.SubprocessError):
        ts_ip = []
    lines = []
    if name and ts_ip and ts_ip[0] in addresses:
        lines.append(f"FORM URL (Tailscale DNS): {scheme}://{name}:{port}/?t={token}")
    for a in addresses:
        label = "Tailscale IP" if a in ts_ip else ("LAN" if not a.startswith("127.") else "this machine")
        lines.append(f"FORM URL ({label}): {scheme}://{a}:{port}/?t={token}")
    order = {"Tailscale DNS": 0, "Tailscale IP": 1, "LAN": 2}
    return sorted(lines, key=lambda l: order.get(l.split("(", 1)[1].split(")", 1)[0], 3))


def lan_address() -> str:
    out = subprocess.run(["ip", "-4", "route", "get", "1.1.1.1"], capture_output=True, text=True).stdout.split()
    if "src" in out:
        return out[out.index("src") + 1]
    raise SystemExit("--bind lan: no default route")


def resolve(bind: str) -> str:
    return {"tailnet": tailnet_address, "lan": lan_address}.get(bind, lambda: bind)()


def read(path: str | None) -> str | None:
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return fh.read().replace("\r\n", "\n")


# ---------------------------------------------------------------- read-only listings

def frontmatter(text: str) -> dict:
    """name/description (and other simple keys) from a leading --- block; folded values joined."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}
    out: dict = {}
    i = 1
    while i < len(lines) and lines[i].strip() != "---":
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", lines[i])
        i += 1
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if val in ("", ">", "|", ">-", "|-", ">+", "|+"):
            parts = []
            while i < len(lines) and lines[i].strip() != "---" and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
                parts.append(lines[i].strip())
                i += 1
            val = " ".join(p for p in parts if p)
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[key] = val
    return out


def first_line(text: str) -> str:
    for ln in text.split("\n"):
        ln = ln.strip().lstrip("#/-*; ").strip()
        if ln and ln != "---" and not ln.startswith("!"):
            return ln[:300]
    return ""


def safe_read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(65536).replace("\r\n", "\n")
    except OSError:
        return ""


def listdir(path: str) -> list[str]:
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def entry(name: str, scope: str, path: str, desc: str) -> dict:
    return {"name": name, "tag": scope, "text": (desc or "(no description)") + "\n\n" + path}


def list_skills(root: str, scope: str) -> list[dict]:
    out = []
    for d in listdir(os.path.join(root, "skills")):
        p = os.path.join(root, "skills", d, "SKILL.md")
        if os.path.isfile(p):
            fm = frontmatter(safe_read(p))
            out.append(entry(fm.get("name") or d, scope, p, fm.get("description", "")))
    return out


def list_md(root: str, sub: str, scope: str) -> list[dict]:
    """*.md files under root/sub (recursively; subfolders give `folder/name`)."""
    out = []
    base = os.path.join(root, sub)
    for dp, dns, fns in os.walk(base):
        dns.sort()
        for fn in sorted(fns):
            if fn.endswith(".md"):
                p = os.path.join(dp, fn)
                text = safe_read(p)
                rel = os.path.relpath(p, base)[:-3]
                fm = frontmatter(text)
                out.append(entry(fm.get("name") or rel, scope, p, fm.get("description") or first_line(text.split("---", 2)[-1] if text.startswith("---") else text)))
    return out


def list_workflows(root: str, scope: str) -> list[dict]:
    out = []
    base = os.path.join(root, "workflows")
    for n in listdir(base):
        p = os.path.join(base, n)
        if os.path.isdir(p):
            out.append(entry(n, scope, p, "(folder: " + ", ".join(listdir(p)[:8]) + ")"))
        else:
            text = safe_read(p)
            out.append(entry(n, scope, p, frontmatter(text).get("description") or first_line(text)))
    return out


def describe_mcp(cfg: object) -> str:
    if not isinstance(cfg, dict):
        return json.dumps(cfg)
    lines = []
    if cfg.get("type"):
        lines.append(f"type: {cfg['type']}")
    if cfg.get("url"):
        lines.append(f"url: {cfg['url']}")
    if cfg.get("command"):
        lines.append("command: " + " ".join([str(cfg["command"])] + [str(a) for a in cfg.get("args") or []]))
    for k in ("env", "headers"):  # values may be secrets: show the names only
        if isinstance(cfg.get(k), dict) and cfg[k]:
            lines.append(f"{k}: " + ", ".join(cfg[k]) + " (values hidden)")
    rest = [k for k in cfg if k not in ("type", "url", "command", "args", "env", "headers")]
    if rest:
        lines.append("other keys: " + ", ".join(rest))
    return "\n".join(lines) or "(empty)"


def list_mcp(sources: list[tuple[str, str, str | None]]) -> list[dict]:
    """sources: (label, path, text-or-None to read from path)."""
    out = []
    for label, path, text in sources:
        text = text if text is not None else read(os.path.realpath(path))
        if not text:
            continue
        try:
            servers = json.loads(text).get("mcpServers")
        except (ValueError, AttributeError):
            continue
        if isinstance(servers, dict):
            for name, cfg in servers.items():
                out.append({"name": name, "tag": label, "text": describe_mcp(cfg) + "\n\n" + path})
    return out


# ---------------------------------------------------------------- project header, feedback, wait

def git(directory: str, *a: str) -> str:
    try:
        r = subprocess.run(["git", "-C", directory, *a], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def owner_repo(url: str) -> str:
    m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?$", url)
    return m.group(1) if m else url


def project_info(directory: str) -> dict:
    top = git(directory, "rev-parse", "--show-toplevel")
    if not top:
        return {"git": False, "dir": directory}
    branch = git(top, "rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        branch = "(detached at " + git(top, "rev-parse", "--short", "HEAD") + ")"
    remote = git(top, "remote", "get-url", "origin")
    return {"git": True, "dir": directory, "top": top, "branch": branch, "remote": owner_repo(remote) if remote else ""}


def load_feedback(state_dir: str) -> list[dict]:
    out = []
    try:
        with open(os.path.join(state_dir, FEEDBACK_FILE), encoding="utf-8") as fh:
            for ln in fh:
                if ln.strip():
                    out.append(json.loads(ln))
    except (OSError, ValueError):
        pass
    return out


def write_json_atomic(path: str, obj: object) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
    os.replace(tmp, path)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="config_form.py wait", description="block until the user sends feedback in the form")
    ap.add_argument("--state-dir", required=True)
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = ap.parse_args(argv)
    sd = os.path.abspath(args.state_dir)
    seen_path = os.path.join(sd, "wait-seen")
    deadline = time.monotonic() + args.timeout
    while True:
        try:
            seen = int(open(seen_path).read().strip() or 0)
        except (OSError, ValueError):
            seen = 0
        new = [f for f in load_feedback(sd) if f.get("n", 0) > seen]
        if new:
            try:
                st = json.load(open(os.path.join(sd, STATE_FILE)))
            except (OSError, ValueError):
                st = {}
            for f in new:
                print(f"FEEDBACK {f['n']} ({f.get('time', '')}):\n{f.get('text', '')}\n")
            print("PROPOSAL FILES TO EDIT (the open form reloads them when they change):")
            for k, v in (st.get("proposals") or {}).items():
                print(f"  {k}: {v}")
            print(f"  user request: {st.get('request_file') or '(none)'}")
            with open(seen_path, "w") as fh:
                fh.write(str(new[-1]["n"]))
            return 0
        try:
            st = json.load(open(os.path.join(sd, STATE_FILE)))
        except (OSError, ValueError):
            st = None
        if st and (st.get("finished") or (st.get("pid") and not pid_alive(st["pid"]))):
            print("the form has ended; no more feedback")
            return 4
        if time.monotonic() >= deadline:
            print("timeout; no new feedback")
            return 3
        time.sleep(1)


PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude config</title><style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{font-family:system-ui,sans-serif;margin:0;background:#111;color:#eee}
button{font:inherit;font-size:15px;padding:8px 14px;background:#2a2a2a;color:#eee;border:1px solid #555;border-radius:4px;cursor:pointer}
button:disabled{opacity:.4;cursor:not-allowed}
button.on{background:#2d4f7c;border-color:#5b8fd0}
button.go{background:#2c6b3a;border-color:#4a9c5a}
#top{display:flex;align-items:center;gap:8px;padding:8px 12px;background:#1b1b1b;border-bottom:1px solid #333}
#top b{flex:1}
#wrap{display:flex;gap:12px;align-items:flex-start;padding:10px 12px}
#navbox{width:290px;flex:none;position:sticky;top:0;max-height:100vh;overflow:auto;border:1px solid #333;background:#171717}
#navbox>summary{padding:8px;cursor:pointer;font-weight:600}
#nav details>summary{display:flex;gap:6px;align-items:center;padding:6px 8px;background:#1d1d1d;border-top:1px solid #2a2a2a;cursor:pointer;font-size:14px}
#nav summary .cnt{margin-left:auto;color:#888;font-size:12px}
#nav input{width:18px;height:18px;flex:none}
#nav a{display:block;padding:4px 8px;font-size:13px;color:#ccc;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;border-bottom:1px solid #202020}
#nav a:hover{background:#252525}
#nav .chg,#nav .new,#nav .del{color:#f0b040;font-weight:600}
#nav a.chg::after,#nav a.new::after,#nav a.del::after,#nav summary.chg .nm::after{content:" \25CF";color:#e0a030}
#main{flex:1;min-width:0}
.comp{border:1px solid #333;margin:0 0 14px;background:#151515}
.comp.hide{display:none}
.comp>h2{margin:0;padding:8px 10px;font-size:16px;background:#1d1d1d;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.comp>h2 .chgmark{color:#e0a030;font-size:12px}
.cb{padding:8px 10px}
.path{color:#999;font-size:12px;margin:0 0 8px;word-break:break-all}
.tabs{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:8px;align-items:center}
.tabs .sp{flex:1}
details.sec{border:1px solid #2a2a2a;margin:0 0 5px;background:#171717}
details.sec>summary{padding:7px 9px;cursor:pointer;font-size:14px;display:flex;gap:8px;align-items:center}
details.sec>summary .tag{color:#888;font-size:11px;border:1px solid #444;border-radius:3px;padding:0 5px}
details.sec.chg,details.sec.new,details.sec.del{border-color:#c08a20;background:#221c0e}
details.sec>.sb{padding:6px 9px 9px}
details.sec details.sec{margin-left:10px}
.cols{display:flex;gap:10px;align-items:flex-start}.cols>div{flex:1;min-width:0}
.lab{font-size:11px;color:#999;text-transform:uppercase;margin:0 0 3px}
pre{margin:0;font:12px/17px ui-monospace,monospace;white-space:pre-wrap;word-break:break-word;background:#101010;border:1px solid #2a2a2a;padding:6px;max-height:60vh;overflow:auto}
pre span{display:block;min-height:17px}
pre .add{background:#12331b;color:#bff0c8}pre .rm{background:#3a1717;color:#f3b6b6}
.note{color:#e0a030;font-size:12px;margin:0 0 5px}
.none{color:#888;padding:8px}
textarea{width:100%;height:70vh;font:13px/18px ui-monospace,monospace;background:#1b1b1b;color:#eee;border:1px solid #444;padding:8px;white-space:pre;overflow:auto;resize:vertical;display:none}
textarea.on{display:block}
textarea[readonly]{background:#161616;color:#ccc}
.view{display:none}.view.on{display:block}
#proj{padding:8px 12px;background:#161c24;border-bottom:1px solid #333;font-size:14px}
#proj .k{color:#999;font-size:11px;text-transform:uppercase;margin-right:4px}#proj span.i{margin-right:18px;word-break:break-all}
#req,#fb{margin:10px 12px 0;border:1px solid #333;background:#151515;padding:8px 10px}
#req h3,#fb h3{margin:0 0 6px;font-size:14px}
#req pre{max-height:30vh}
#fb textarea{display:block;height:90px;white-space:pre-wrap;resize:vertical;margin-bottom:6px}
#fbsent{margin-top:8px;font-size:13px;color:#bbb}#fbsent div{border-left:3px solid #444;padding:2px 8px;margin-top:4px;white-space:pre-wrap}
#fbstat{margin-left:10px;color:#e0a030;font-size:13px}
.newprop{color:#e0a030;font-size:12px}
.out{white-space:pre-wrap;margin-top:10px;padding:8px;border-left:3px solid #555;display:none}
.out.err{border-color:#d44;color:#f99;display:block}.out.ok{border-color:#4a9c5a;display:block}
@media(max-width:700px){#wrap{flex-direction:column;padding:8px}#navbox{width:100%;position:static;max-height:45vh}.cols{flex-direction:column}textarea{height:55vh}button{padding:7px 10px}}
</style></head><body>
<div id="top"><b>Claude config</b><button id="done">Done</button></div>
<div id="proj"></div><div id="req" hidden><h3>User request</h3><pre id="reqtext"></pre></div>
<div id="fb"><h3>Feedback for Claude</h3><textarea id="fbtext" placeholder="Tell Claude what to change in the proposals" spellcheck="false"></textarea>
<button class="go" id="fbsend">Submit feedback</button><span id="fbstat"></span><div id="fbsent"></div></div>
<div id="wrap"><details id="navbox" open><summary>Components</summary><div id="nav"></div></details><div id="main"></div></div>
<script type="application/json" id="data">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('data').textContent);
const VIEWS = [['sec','Sections'],['cur','Current'],['pro','Proposed'],['fin','Final']];
const $ = (s, r=document) => r.querySelector(s);
const mk = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const LABEL = {chg: 'changed', new: 'new', del: 'removed'};

// ---- sections: items {title, level, cur, pro, status, children?, note?}; status same|chg|new|del
function st(cur, pro, hasPro) {
  if (!hasPro) return 'same';
  if (cur == null && pro == null) return 'same';
  if (cur == null) return 'new';
  if (pro == null) return 'del';
  return cur === pro ? 'same' : 'chg';
}
function mdParse(text) {
  const lines = text.split('\n'), out = []; let fence = false;
  lines.forEach((l, i) => {
    if (/^\s*(```|~~~)/.test(l)) { fence = !fence; return; }
    const m = !fence && /^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$/.exec(l);
    if (m) out.push({title: m[2], level: m[1].length, line: i});
  });
  const first = out.length ? out[0].line : lines.length;
  if (lines.slice(0, first).some(l => l.trim())) out.unshift({title: '(top)', level: 0, line: 0});
  const seen = {};
  out.forEach((s, k) => {
    const end = k + 1 < out.length ? out[k + 1].line : lines.length;
    s.body = lines.slice(s.line, end).join('\n').replace(/\s+$/, '');
    const id = s.title + '#' + s.level; seen[id] = (seen[id] || 0) + 1; s.key = id + '#' + seen[id];
  });
  return out;
}
function mdItems(curText, proText, hasPro) {
  const c = mdParse(curText), cm = new Map(c.map(s => [s.key, s]));
  if (!hasPro) return c.map(s => ({title: s.title, level: s.level, cur: s.body, status: 'same'}));
  const out = [], used = new Set();
  mdParse(proText).forEach(s => {
    const o = cm.get(s.key); used.add(s.key);
    out.push({title: s.title, level: s.level, cur: o ? o.body : null, pro: s.body, status: st(o ? o.body : null, s.body, true)});
  });
  c.forEach(s => { if (!used.has(s.key)) out.push({title: s.title, level: s.level, cur: s.body, pro: null, status: 'del'}); });
  return out;
}
const isObj = v => v && typeof v === 'object' && !Array.isArray(v);
const js = v => v === undefined ? null : JSON.stringify(v, null, 2);
const GROUPS = ['permissions', 'hooks'];
function jsonItems(curText, proText, hasPro) {
  let c, p;
  try { c = curText.trim() ? JSON.parse(curText) : {}; } catch (e) { c = null; }
  if (hasPro) { try { p = JSON.parse(proText); } catch (e) { p = null; } } else p = {};
  if (!isObj(c) || !isObj(p)) {
    const it = {title: '(whole file)', level: 1, cur: curText, status: st(curText, hasPro ? proText : null, hasPro), note: 'Not a parseable JSON object, so shown as plain text.'};
    if (hasPro) it.pro = proText;
    return [it];
  }
  const keys = Object.keys(c).concat(hasPro ? Object.keys(p).filter(k => !(k in c)) : []);
  return keys.map(k => {
    const vc = c[k], vp = hasPro ? p[k] : undefined;
    if (GROUPS.includes(k) && (vc === undefined || isObj(vc)) && (vp === undefined || isObj(vp)) && (vc !== undefined || vp !== undefined)) {
      const sub = Object.keys(vc || {}).concat(Object.keys(vp || {}).filter(x => !(vc || {}).hasOwnProperty(x)));
      const ch = sub.map(x => ({title: x, level: 2, cur: js((vc || {})[x]), pro: hasPro ? js((vp || {})[x]) : undefined,
        status: st(js((vc || {})[x]), hasPro ? js((vp || {})[x]) : null, hasPro)}));
      const sts = ch.map(x => x.status);
      return {title: k, level: 1, children: ch, status: sts.length && sts.every(s => s === 'new') ? 'new' : sts.length && sts.every(s => s === 'del') ? 'del' : sts.some(s => s !== 'same') ? 'chg' : 'same'};
    }
    return {title: k, level: 1, cur: js(vc), pro: hasPro ? js(vp) : undefined, status: st(js(vc), hasPro ? js(vp) : null, hasPro)};
  });
}
function edItems(ed) {
  const hasPro = ed.d.proposed != null, f = ed.d.kind === 'md' ? mdItems : jsonItems;
  return f(ed.cur, hasPro ? ed.d.proposed : '', hasPro);
}

// ---- rendering
let uid = 0;
const COMPS = [];
function numbered(items) { items.forEach(it => { it.id = 's' + (++uid); (it.children || []).forEach(c => numbered([c])); }); return items; }
function flat(items, out = []) { items.forEach(it => { out.push(it); flat(it.children || [], out); }); return out; }

function fillPre(pre, text, other, cls) {
  const o = new Set((other == null ? [] : other.split('\n')).map(x => x.trimEnd()));
  text.split('\n').forEach(l => { const s = mk('span', other === undefined || o.has(l.trimEnd()) ? '' : cls, l); pre.append(s); });
}
function col(label, text, other, cls) {
  const d = mk('div'); d.append(mk('div', 'lab', label)); const pre = mk('pre'); fillPre(pre, text, other, cls); d.append(pre); return d;
}
function renderItem(it) {
  const d = mk('details', 'sec ' + it.status); d.id = it.id;
  d.open = it.status !== 'same';
  const sm = mk('summary'); sm.append(mk('span', '', it.title));
  if (it.tag) sm.append(mk('span', 'tag', it.tag));
  if (LABEL[it.status]) sm.append(mk('span', 'tag', LABEL[it.status]));
  d.append(sm);
  const b = mk('div', 'sb'); d.append(b);
  if (it.note) b.append(mk('div', 'note', it.note));
  if (it.children) { if (!it.children.length) b.append(mk('div', 'none', '(empty)')); it.children.forEach(c => b.append(renderItem(c))); return d; }
  if (it.status === 'chg') {
    const cols = mk('div', 'cols');
    cols.append(col('Proposed', it.pro, it.cur, 'add'), col('Current', it.cur, it.pro, 'rm'));
    b.append(cols);
  } else if (it.status === 'new') b.append(col('Proposed (new)', it.pro, null, 'add'));
  else if (it.status === 'del') b.append(col('Current (the proposal removes it)', it.cur, null, 'rm'));
  else b.append(col(it.pro !== undefined ? 'Current (unchanged by the proposal)' : 'Current', it.cur, undefined, ''));
  return d;
}
function setSecs(comp) {
  comp.items = numbered(comp.getItems());
  comp.secBox.textContent = '';
  if (!comp.items.length) comp.secBox.append(mk('div', 'none', comp.empty || 'No sections.'));
  comp.items.forEach(it => comp.secBox.append(renderItem(it)));
  comp.changed = flat(comp.items).filter(i => i.status !== 'same').length;
  comp.mark.textContent = comp.changed ? comp.changed + ' changed' : '';
}
function renderNav() {
  const nav = $('#nav'), openSet = new Set([...nav.querySelectorAll('details[open]')].map(x => x.dataset.c));
  const first = !nav.children.length;
  nav.textContent = '';
  COMPS.forEach(c => {
    const d = mk('details'); d.dataset.c = c.id;
    d.open = first ? c.changed > 0 : openSet.has(c.id);
    const sm = mk('summary', c.changed ? 'chg' : ''), cb = mk('input'); cb.type = 'checkbox'; cb.checked = !c.hidden; cb.title = 'Show in the main view';
    cb.onclick = e => { e.stopPropagation(); setHidden(c, !cb.checked); };
    cb.dataset.c = c.id;
    const nm = mk('span', 'nm', c.label), n = flat(c.items).length;
    sm.append(cb, nm, mk('span', 'cnt', c.changed ? c.changed + ' changed' : String(n)));
    d.append(sm);
    flat(c.items).forEach(it => {
      const a = mk('a', it.status === 'same' ? '' : it.status, it.title + (it.tag ? ' [' + it.tag + ']' : ''));
      a.style.paddingLeft = (14 + Math.max(0, it.level - 1) * 12) + 'px'; a.title = it.title;
      a.onclick = () => goto(c, it);
      d.append(a);
    });
    if (!c.items.length) d.append(mk('div', 'none', 'none'));
    nav.append(d);
  });
}
function setHidden(c, h) { c.hidden = h; c.el.classList.toggle('hide', h); const cb = $('#nav input[data-c="' + c.id + '"]'); if (cb) cb.checked = !h; }
function goto(c, it) {
  setHidden(c, false);
  if (c.setView) c.setView('sec');
  const el = document.getElementById(it.id); if (!el) return;
  for (let p = el; p; p = p.parentElement && p.parentElement.closest('details')) p.open = true;
  el.scrollIntoView({block: 'start', behavior: 'smooth'});
}

function newComp(id, label, extra) {
  const c = Object.assign({id, label, hidden: false, items: [], changed: 0}, extra);
  c.el = mk('section', 'comp'); c.el.id = 'c-' + id;
  const h = mk('h2', '', label); c.mark = mk('span', 'chgmark'); h.append(c.mark); c.el.append(h);
  c.body = mk('div', 'cb'); c.el.append(c.body);
  COMPS.push(c); $('#main').append(c.el);
  return c;
}
function buildEditor(d) {
  const ed = newComp(d.id, d.name, {d, cur: d.current, ta: {}, views: {}, tabBtn: {}});
  ed.getItems = () => edItems(ed);
  ed.body.append(mk('div', 'path', d.path + (d.exists ? '' : '  (does not exist yet)')));
  const tabs = mk('div', 'tabs'); ed.body.append(tabs);
  VIEWS.forEach(([k, label]) => { const t = mk('button', '', label); t.onclick = () => ed.setView(k); ed.tabBtn[k] = t; tabs.append(t); });
  tabs.append(mk('span', 'sp'));
  ed.useBtn = mk('button', '', 'Use proposed'); ed.useBtn.disabled = d.proposed == null; ed.useBtn.onclick = () => useProposed(ed);
  ed.subBtn = mk('button', 'go', 'Submit ' + d.name); ed.subBtn.onclick = () => submit(ed);
  tabs.append(ed.useBtn, ed.subBtn);
  ed.secBox = mk('div', 'view'); ed.views.sec = ed.secBox; ed.body.append(ed.secBox);
  const text = {cur: d.current, pro: d.proposed == null ? 'No proposal was given for ' + d.name + '.' : d.proposed, fin: d.current};
  ['cur', 'pro', 'fin'].forEach(k => {
    const v = mk('div', 'view'), ta = mk('textarea', 'on'); ta.value = text[k]; ta.spellcheck = false; ta.wrap = 'off'; if (k !== 'fin') ta.readOnly = true;
    v.append(ta); ed.ta[k] = ta; ed.views[k] = v; ed.body.append(v);
  });
  ed.ta.fin.addEventListener('input', () => dirty(ed));
  ed.out = mk('div', 'out'); ed.body.append(ed.out);
  ed.setView = k => { VIEWS.forEach(([v]) => { ed.views[v].classList.toggle('on', v === k); ed.tabBtn[v].classList.toggle('on', v === k); }); };
  ed.setView('sec'); setSecs(ed);
  return ed;
}
function buildList(l) {
  const c = newComp(l.id, l.name, {empty: 'None found.'});
  c.getItems = () => l.items.map(i => ({title: i.name, tag: i.tag, level: 1, cur: i.text, status: 'same'}));
  c.body.append(c.secBox = mk('div'));
  setSecs(c);
  return c;
}
function dirty(ed) { ed.tabBtn.fin.textContent = 'Final' + (ed.ta.fin.value !== ed.cur ? ' ●' : ''); }
function useProposed(ed) {
  if (ed.d.proposed == null) return;
  if (ed.ta.fin.value !== ed.cur && ed.ta.fin.value !== ed.d.proposed && !confirm('Replace your Final edits with the proposal?')) return;
  ed.ta.fin.value = ed.d.proposed; dirty(ed); ed.setView('fin');
}
function say(ed, cls, msg) { ed.out.className = 'out ' + cls; ed.out.textContent = msg; }
async function submit(ed) {
  ed.subBtn.disabled = true;
  try {
    const r = await fetch(location.href, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({file: ed.d.id, text: ed.ta.fin.value})});
    const j = await r.json();
    say(ed, r.ok ? 'ok' : 'err', j.message);
    if (j.current != null) { ed.cur = j.current; ed.ta.cur.value = j.current; setSecs(ed); renderNav(); }
    dirty(ed);
    if (j.finished) document.body.textContent = j.message;
  } catch (e) { say(ed, 'err', 'Request failed: ' + e); }
  ed.subBtn.disabled = false;
}
// ---- header, request, feedback, polling for revised proposals
function showProject(p) {
  const b = $('#proj'); b.textContent = '';
  const add = (k, v) => { const s = mk('span', 'i'); s.append(mk('span', 'k', k), document.createTextNode(v)); b.append(s); };
  if (p.git) { add('Repository', p.top + (p.remote ? '  (' + p.remote + ')' : '')); add('Branch', p.branch); }
  else add('Directory', p.dir + '  (not a git repository)');
}
function showFeedback(fb) {
  const box = $('#fbsent'); box.textContent = '';
  fb.forEach(f => box.append(mk('div', '', '#' + f.n + ' ' + f.time + '\n' + f.text)));
}
showProject(D.project);
if (D.request != null && D.request.trim()) { $('#req').hidden = false; $('#reqtext').textContent = D.request; }
let fbCount = D.feedback.length, waiting = false;
showFeedback(D.feedback);
$('#fbsend').onclick = async () => {
  const t = $('#fbtext').value; if (!t.trim()) return;
  $('#fbsend').disabled = true;
  try {
    const r = await fetch(location.href, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({action: 'feedback', text: t})});
    const j = await r.json();
    if (r.ok) { $('#fbtext').value = ''; $('#fbstat').textContent = j.message; waiting = true; fbCount = j.feedback.length; showFeedback(j.feedback); }
    else $('#fbstat').textContent = j.message;
  } catch (e) { $('#fbstat').textContent = 'Request failed: ' + e; }
  $('#fbsend').disabled = false;
};
function applyProposal(ed, text) {
  const old = ed.d.proposed;
  if (text == null || text === old) return false;
  const edited = ed.ta.fin.value !== ed.cur && ed.ta.fin.value !== old;
  ed.d.proposed = text; ed.ta.pro.value = text; ed.useBtn.disabled = false;
  setSecs(ed);
  if (!edited) { ed.ta.fin.value = ed.cur; }
  if (edited) { if (!ed.newMark) { ed.newMark = mk('span', 'newprop', 'A newer proposal is available; your Final edits were kept (use "Use proposed" to take it).'); ed.tabBtn.fin.parentElement.after(ed.newMark); } }
  else if (ed.newMark) { ed.newMark.remove(); ed.newMark = null; }
  dirty(ed);
  return true;
}
let sig = D.sig;
setInterval(async () => {
  try {
    const r = await fetch('/state' + location.search); if (!r.ok) return;
    const j = await r.json();
    showProject(j.project);
    if (j.feedback.length !== fbCount) { fbCount = j.feedback.length; showFeedback(j.feedback); }
    if (j.sig === sig) return;
    sig = j.sig;
    let any = false;
    D.editors.forEach(d => { const ed = COMPS.find(c => c.id === d.id); if (ed && applyProposal(ed, j.proposals[d.id])) any = true; });
    if (any) { renderNav(); if (waiting) { $('#fbstat').textContent = 'Revised proposals received (' + new Date().toLocaleTimeString() + ').'; waiting = false; } }
  } catch (e) { /* server busy or gone: try again */ }
}, 4000);
D.editors.forEach(buildEditor);
D.lists.forEach(buildList);
renderNav();
if (innerWidth < 700) $('#navbox').open = false;
$('#done').onclick = async () => {
  if (!confirm('End the editor? Unsubmitted edits are lost.')) return;
  const r = await fetch(location.href, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({action: 'done'})});
  document.body.textContent = (await r.json()).message;
};
</script></body></html>"""


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "wait":
        return wait_main(sys.argv[2:])
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bind", action="append", help="127.0.0.1 (default), `tailnet`, `lan`, or one address; repeatable")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="seconds before the form exits (default 8 hours)")
    ap.add_argument("--project-dir", help="the directory this form is for (default: the current directory); its git repository and branch are shown")
    ap.add_argument("--request-file", help="file with the user's request, shown read-only at the top")
    ap.add_argument("--request", help="the user's request as text (when no --request-file)")
    ap.add_argument("--state-dir", help="where feedback and the state file go (default: a new folder under " + STATE_ROOT + ")")
    ap.add_argument("--proposed-claude-md", help="file with Claude's proposed user CLAUDE.md")
    ap.add_argument("--proposed-settings", help="file with Claude's proposed settings.json")
    ap.add_argument("--project-claude-md", help="the repository's CLAUDE.md (adds a project editor and the project's listings)")
    ap.add_argument("--proposed-project-claude-md", help="file with Claude's proposed project CLAUDE.md")
    args = ap.parse_args()
    binds = args.bind or ["127.0.0.1"]
    if any(b in ("0.0.0.0", "::", "") for b in binds):
        print("refusing to listen on all interfaces", file=sys.stderr)
        return 2
    if args.proposed_project_claude_md and not args.project_claude_md:
        print("--proposed-project-claude-md needs --project-claude-md", file=sys.stderr)
        return 2
    conf = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    editors = [
        {"id": "claude", "name": "CLAUDE.md", "kind": "md", "path": os.path.join(conf, "CLAUDE.md"), "proposed_path": args.proposed_claude_md, "backup": "CLAUDE.md"},
        {"id": "settings", "name": "settings.json", "kind": "json", "path": os.path.join(conf, "settings.json"), "proposed_path": args.proposed_settings, "backup": "settings.json"},
    ]
    project = None
    if args.project_claude_md:
        ppath = os.path.abspath(args.project_claude_md)
        project = os.path.dirname(ppath)
        editors.insert(1, {"id": "project", "name": "Project CLAUDE.md", "kind": "md", "path": ppath, "proposed_path": args.proposed_project_claude_md, "backup": "project-CLAUDE.md"})
        editors[0]["name"] = "User CLAUDE.md"
    for e in editors:
        if e["proposed_path"] and read(e["proposed_path"]) is None:
            print(f"proposal file not found: {e['proposed_path']}", file=sys.stderr)
            return 2
    by_id = {e["id"]: e for e in editors}
    project_dir = os.path.abspath(args.project_dir or os.getcwd())
    state_dir = os.path.abspath(args.state_dir or os.path.join(STATE_ROOT, time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())))
    os.makedirs(state_dir, exist_ok=True)
    request_file = os.path.abspath(args.request_file) if args.request_file else None
    state_file = os.path.join(state_dir, STATE_FILE)

    def write_state(finished: bool = False) -> None:
        write_json_atomic(state_file, {
            "state_dir": state_dir, "pid": os.getpid(), "finished": finished, "project_dir": project_dir,
            "request_file": request_file,
            "proposals": {e["id"]: os.path.abspath(e["proposed_path"]) for e in editors if e["proposed_path"]},
        })

    def request_text() -> str | None:
        return read(request_file) if request_file else args.request

    cache: dict = {"sig": None, "props": {}}

    def proposals() -> tuple[str, dict]:
        """(signature of the proposal files' mtimes, {id: text|None}); re-read only when it changes."""
        sig = repr([(e["id"], os.stat(e["proposed_path"]).st_mtime_ns if e["proposed_path"] and os.path.exists(e["proposed_path"]) else None) for e in editors])
        if sig != cache["sig"]:
            cache["sig"] = sig
            cache["props"] = {e["id"]: read(e["proposed_path"]) for e in editors}
        return sig, cache["props"]
    token = secrets.token_urlsafe(24)
    state = {"done": False}
    write_state()
    replaced: dict[str, dict] = {}  # id -> {"backup": path|None, "chars": n}

    def listings() -> list[dict]:
        scopes = [("personal", conf)] + ([("project", os.path.join(project, ".claude"))] if project else [])
        mcp_src: list[tuple[str, str, str | None]] = [("~/.mcp.json", os.path.expanduser("~/.mcp.json"), None)]
        if project:
            mcp_src.append(("project .mcp.json", os.path.join(project, ".mcp.json"), None))
        mcp_src.append(("settings.json", by_id["settings"]["path"], read(by_id["settings"]["path"])))
        mcp_src = [(label, path, text) for label, path, text in mcp_src]
        lists = [{"id": "mcp", "name": "MCP servers", "items": list_mcp(mcp_src)}]
        lists.append({"id": "skills", "name": "Skills", "items": [i for s, r in scopes for i in list_skills(r, s)]})
        lists.append({"id": "agents", "name": "Agents", "items": [i for s, r in scopes for i in list_md(r, "agents", s)]})
        lists.append({"id": "workflows", "name": "Dynamic workflows", "items": [i for s, r in scopes for i in list_workflows(r, s)]})
        lists.append({"id": "commands", "name": "Commands", "items": [i for s, r in scopes for i in list_md(r, "commands", s)]})
        return lists

    def data_json() -> str:
        payload = {"editors": [
            {"id": e["id"], "name": e["name"], "kind": e["kind"], "path": e["path"], "exists": os.path.exists(e["path"]),
             "current": read(e["path"]) or "", "proposed": proposals()[1][e["id"]]} for e in editors],
            "lists": listings(), "project": project_info(project_dir), "request": request_text(),
            "feedback": load_feedback(state_dir), "sig": proposals()[0]}
        return json.dumps(payload).replace("<", "\\u003c").replace("&", "\\u0026")

    def backup_path(name: str) -> str:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        base = f"/tmp/claude-config-backup-{stamp}-{name}"
        path, n = base, 1
        while os.path.exists(path):  # never write into an existing backup
            n += 1
            path = f"{base}.{n}"
        return path

    def replace_file(e: dict, new: str) -> str:
        path = e["path"]
        backup = None
        lines = []
        if os.path.exists(path):
            backup = backup_path(e["backup"])
            with open(path, "rb") as src, open(backup, "xb") as dst:
                dst.write(src.read())
            lines.append(f"Backed up {path} -> {backup}")
        tmp = path + ".form-tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(new)
            fh.flush()
            os.fsync(fh.fileno())
        if os.path.exists(path):
            os.chmod(tmp, os.stat(path).st_mode & 0o7777)
        os.replace(tmp, path)
        lines.append(f"Replaced {path} ({len(new)} characters)")
        replaced[e["id"]] = {"path": path, "backup": backup, "chars": len(new)}
        return "\n".join(lines)

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, text: str, ctype: str = "text/plain; charset=utf-8") -> None:
            data = text.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(data)

        def _json(self, code: int, **kw) -> None:
            self._send(code, json.dumps(kw), "application/json; charset=utf-8")

        def do_GET(self):
            if self.path == f"/state?t={token}":
                sig, props = proposals()
                return self._json(200, sig=sig, proposals=props, feedback=load_feedback(state_dir), project=project_info(project_dir))
            if self.path != f"/?t={token}":
                return self._send(404, "not found")
            self._send(200, PAGE.replace("__DATA__", data_json()), "text/html; charset=utf-8")

        def do_POST(self):
            if self.path != f"/?t={token}":
                return self._send(404, "not found")
            origin = self.headers.get("Origin")
            host = self.headers.get("Host", "")
            if origin and origin not in (f"http://{host}", f"https://{host}"):
                return self._send(403, "wrong origin")
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            if n <= 0 or n > MAX_BODY:
                return self._send(413, "nothing sent, or too large")
            try:
                sent = json.loads(self.rfile.read(n))
                assert isinstance(sent, dict)
            except Exception:
                return self._json(400, message="could not read the form")
            if sent.get("action") == "done":
                state["done"] = True
                return self._json(200, message="Done; the editor has ended. You can close this tab.")
            if sent.get("action") == "feedback":
                text = sent.get("text")
                if not isinstance(text, str) or not text.strip():
                    return self._json(400, message="no feedback text")
                fb = load_feedback(state_dir)
                rec = {"n": len(fb) + 1, "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "text": text.replace("\r\n", "\n")}
                with open(os.path.join(state_dir, FEEDBACK_FILE), "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                print(f"feedback {rec['n']} stored in {state_dir}", flush=True)
                return self._json(200, message="Sent to Claude; waiting for revised proposals", feedback=load_feedback(state_dir))
            e = by_id.get(str(sent.get("file", "")))
            if e is None or not isinstance(sent.get("text"), str):
                return self._json(400, message="unknown file or no text sent")
            new = sent["text"].replace("\r\n", "\n")
            old = read(e["path"]) or ""
            if new == old:
                return self._json(200, message="No changes; nothing to do.", current=old)
            if e["kind"] == "json":
                try:
                    json.loads(new)
                except json.JSONDecodeError as err:
                    return self._json(400, message=f"{e['name']} is not valid JSON (line {err.lineno}, column {err.colno}): {err.msg}. Nothing replaced.")
            try:
                msg = replace_file(e, new)
            except OSError as err:
                return self._json(500, message=f"could not replace {e['name']}: {err}")
            print(msg, flush=True)
            if len(replaced) == len(editors):
                state["done"] = True
                msg += "\n\nAll files replaced; the editor has ended. You can close this tab."
            self._json(200, message=msg, current=new, finished=state["done"])

    addresses = list(dict.fromkeys(resolve(b) for b in binds))
    servers = []
    for address in addresses:
        try:
            servers.append(http.server.HTTPServer((address, args.port), Handler))
        except OSError as e:
            print(f"cannot listen on {address}:{args.port}: {e}", file=sys.stderr)
            for srv in servers:
                srv.server_close()
            return 1
    print(f"STATE DIR: {state_dir}", flush=True)
    for line in link_lines("http", addresses, args.port, token, tailnet_name()):
        print(line, flush=True)
    deadline = time.monotonic() + args.timeout
    by_socket = {srv.socket: srv for srv in servers}
    try:
        while not state["done"] and time.monotonic() < deadline:
            for sock in select.select(list(by_socket), [], [], 1)[0]:
                by_socket[sock].handle_request()
    except KeyboardInterrupt:
        pass
    for srv in servers:
        srv.server_close()
    write_state(finished=True)
    print("SUMMARY:")
    if replaced:
        for r in replaced.values():
            print(f"  replaced {r['path']} ({r['chars']} characters); backup: {r['backup'] or 'none (file did not exist)'}")
    else:
        print("  nothing replaced")
    return 0 if state["done"] else 3


if __name__ == "__main__":
    sys.exit(main())
