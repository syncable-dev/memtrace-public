#!/usr/bin/env bash
#
# memtrace-enforce-stop.sh: Stop-hook backstop for Memtrace.
#
# The PreToolUse gate (memtrace-enforce-pretooluse.sh) denies raw code search
# in an indexed repo; this hook catches what a deny cannot: turns where the
# agent never attempted a search at all, and still finished without using the
# graph. It blocks the agent stop and forces ONE continuation when the last
# turn, inside an indexed repo, did any of:
#
#   (A) code discovery (source-file Read, or Grep content search) with NO
#       memtrace MCP call -> pairs with the PreToolUse deny: "did not grep"
#       becomes "used memtrace". Glob is filename-by-name discovery (out of
#       scope, like find/bfs) and never counts as code discovery here.
#   (B) deleted/refactored an existing symbol (an Edit/MultiEdit that shrinks
#       a definition) with NO decision-memory call (recall_decision /
#       why_is_this_here / get_symbol_context / get_impact /
#       governing_contracts) -> nudge to check WHY the code exists before
#       removing it.
#   (C) leaned on git log / git diff <ref> for catch-up with NO
#       temporal-memory call (get_evolution / get_changes_since /
#       get_timeline) -> nudge to the graph change memory, which is
#       symbol-level and cross-repo where git is not.
#
# Calibrated to never loop or false-positive:
#   - anti-loop: stop_hook_active=true -> allow (one forced continuation only,
#     so each nudge costs at most one extra turn even if detection is
#     generous)
#   - scope: only inside an indexed repo (the enforce list); else allow
#   - fail-open: daemon down -> allow; opt-out MEMTRACE_ENFORCE=off; empty
#     input -> allow
#   - a memfleet__ call satisfies the same graph as memtrace__ (both expose
#     find_code)
#
# The parsed-language set for (A) tracks what Memtrace indexes; extend EXT as
# the indexer gains grammars. Test override: MEMTRACE_ENFORCE_HEALTH=ok|fail
# bypasses the liveness probe (used by the fixture-driven negative-case
# tests; never wire this in a live install).
set -uo pipefail
allow(){ exit 0; }
block(){ python3 -c 'import json,sys;print(json.dumps({"decision":"block","reason":sys.argv[1]}))' "$1"; exit 0; }

[[ "${MEMTRACE_ENFORCE:-on}" == "off" ]] && allow
input="$(cat 2>/dev/null || true)"; [[ -n "$input" ]] || allow

vals="$(printf '%s' "$input" | python3 -c '
import json,sys
try: o=json.load(sys.stdin)
except Exception: o={}
print("true" if o.get("stop_hook_active") else "false")
print(o.get("transcript_path","") or "")
print(o.get("cwd","") or "")
' 2>/dev/null || true)"
ACTIVE="$(sed -n 1p <<<"$vals")"
TRANSCRIPT="$(sed -n 2p <<<"$vals")"
CWD="$(sed -n 3p <<<"$vals")"

[[ "$ACTIVE" == "true" ]] && allow                      # anti-loop
[[ -n "$TRANSCRIPT" && -f "$TRANSCRIPT" ]] || allow

# daemon fail-open: liveness = memcore-server gRPC backend alive (serves
# find_code). The HTTP UI is often down even when memtrace is live; the
# backend process is the reliable signal. Set MEMTRACE_HEALTH_URL to force
# the legacy HTTP probe.
health="${MEMTRACE_ENFORCE_HEALTH:-}"
if [[ -z "$health" ]]; then
  if [[ -n "${MEMTRACE_HEALTH_URL:-}" ]]; then
    curl -sf --max-time 1 "$MEMTRACE_HEALTH_URL" >/dev/null 2>&1 && health=ok || health=fail
  else
    pgrep -f memcore-server >/dev/null 2>&1 && health=ok || health=fail
  fi
fi
[[ "$health" == "ok" ]] || allow

# scope: cwd under an indexed repo root
LIST="${MEMTRACE_ENFORCE_REPOS:-$HOME/.memtrace/enforce-repos}"
[[ -n "$CWD" && -f "$LIST" ]] || allow
in=0
while IFS= read -r line || [[ -n "$line" ]]; do
  root="${line%%#*}"; root="${root#"${root%%[![:space:]]*}"}"; root="${root%"${root##*[![:space:]]}"}"; root="${root%/}"
  [[ -z "$root" ]] && continue
  case "$CWD/" in "$root"/*) in=1; break ;; esac
done < "$LIST"
[[ "$in" -eq 1 ]] || allow

# analyze the last turn; emit one of: allow | discover | decision | evolution
verdict="$(MTE_T="$TRANSCRIPT" python3 -c '
import json,os,re
try: lines=open(os.environ["MTE_T"]).read().splitlines()
except Exception: print("allow"); raise SystemExit
EXT=(".rs",".ts",".tsx",".mts",".cts",".js",".jsx",".mjs",".cjs",".py",".go",
     ".java",".c",".cc",".cpp",".h",".hpp",".rb",".php",".cs",".swift",".kt",
     ".scala",".sh",".lua",".ml",".vue",".svelte",".dart",".ex",".exs")
DEF=re.compile(r"\b(fn|def|defp|function|func|class|impl|trait|interface|struct|enum|module|defmodule|type|const|let|var|public|private|protected)\b")
# last user text turn = turn boundary
start=0
for i,l in enumerate(lines):
    try: o=json.loads(l)
    except Exception: continue
    if o.get("type")=="user":
        c=o.get("message",{}).get("content",[])
        if isinstance(c,str) or (isinstance(c,list) and any(isinstance(b,dict) and b.get("type")=="text" for b in c)):
            start=i
mem=False; decmem=False; temporal=False
cd=False; refactor=False; gitcatch=False
def src(fp): return bool(fp) and fp.endswith(EXT)
for l in lines[start:]:
    try: o=json.loads(l)
    except Exception: continue
    c=o.get("message",{}).get("content",[])
    if not isinstance(c,list): continue
    for b in c:
        if not isinstance(b,dict) or b.get("type")!="tool_use": continue
        n=b.get("name",""); inp=(b.get("input",{}) or {})
        if n.startswith("mcp__memtrace__") or n.startswith("mcp__memfleet__"):
            mem=True
            leaf=n.split("__")[-1]
            if leaf in ("recall_decision","why_is_this_here","get_symbol_context","get_impact","governing_contracts","verify_intent"): decmem=True
            if leaf in ("get_evolution","get_changes_since","get_timeline","replay_history","get_arc","get_cochange_context","get_episode_replay"): temporal=True
        elif n=="Grep": cd=True   # Grep = content search (in scope); Glob = filename-by-name (out of scope, never flagged)
        elif n=="Read":
            if src(inp.get("file_path","") or ""): cd=True
        elif n in ("Edit","MultiEdit"):
            if not src(inp.get("file_path","") or ""): continue
            edits=inp.get("edits") if n=="MultiEdit" else [inp]
            if not isinstance(edits,list): edits=[inp]
            for e in edits:
                if not isinstance(e,dict): continue
                old=e.get("old_string","") or ""; new=e.get("new_string","") or ""
                # a shrink over a definition line = deletion/refactor of a symbol
                if DEF.search(old) and (len(new) < len(old)) and (new.strip()=="" or len(new) < 0.7*len(old)):
                    refactor=True
        elif n=="Bash":
            cmd=inp.get("command","") or ""
            if re.search(r"\bgit\s+log\b",cmd) or re.search(r"\bgit\s+diff\b[^|]*(\b[0-9a-f]{7,40}\b|\.\.|HEAD[~^]|origin/)",cmd):
                gitcatch=True
if cd and not mem: print("discover")
elif refactor and not decmem: print("decision")
elif gitcatch and not temporal: print("evolution")
else: print("allow")
' 2>/dev/null || echo allow)"

case "$verdict" in
  discover)
    block "Memtrace is active in this indexed repo and this turn did code discovery (source-file reads or search) without it. Before finishing, use mcp__memtrace__find_code / mcp__memtrace__find_symbol for the code-discovery part instead of raw reads. Opt-out: MEMTRACE_ENFORCE=off."
    ;;
  decision)
    block "Memtrace is active and this turn deleted or refactored an existing symbol without checking why it exists. Before finishing, run mcp__memtrace__recall_decision / mcp__memtrace__why_is_this_here (or get_symbol_context / get_impact) to confirm no decision, ban, or contract governs it. Opt-out: MEMTRACE_ENFORCE=off."
    ;;
  evolution)
    block "Memtrace is active and this turn used git log / git diff to catch up on history. Before finishing, prefer mcp__memtrace__get_evolution / mcp__memtrace__get_changes_since: symbol-level, cross-repo change memory git cannot reconstruct. Opt-out: MEMTRACE_ENFORCE=off."
    ;;
  *) allow ;;
esac
allow
