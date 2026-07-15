#!/usr/bin/env bash
#
# memtrace-enforce-pretooluse.sh: deterministic PreToolUse gate for Memtrace.
#
# Wired as a PreToolUse hook (matcher "Grep|Bash") so that, inside a repository
# Memtrace has indexed, raw recursive code search is redirected to the graph
# tools that answer the same question with caller edges, cross-repo links, and
# decision context attached:
#
#     mcp__memtrace__find_code      find code by meaning / content
#     mcp__memtrace__find_symbol    locate a definition by name
#     mcp__memtrace__get_symbol_context / get_impact   role and blast radius
#
# The bundled skills already ADVISE this redirect (a banner, a skill
# description); they cannot ENFORCE it. This hook is the deterministic lever:
# it denies the search and names the tool to use instead, so the redirect
# happens even when the model reaches for grep out of habit.
#
# ---------------------------------------------------------------------------
# DESIGN CONTRACT
#
#   Fail-open by construction. Any ambiguity resolves to ALLOW. The hook NEVER
#   blocks:
#     - Glob or filename discovery (find/fd/bfs): those locate files by name.
#     - plain grep or a real stdin pipe (cat x | grep pat): stream filtering.
#     - config, data, docs, logs, lockfiles: non-source targets.
#     - any path outside an indexed repository.
#     - anything, when the Memtrace backend is not running.
#   Rationale: a missed search is a cheap regret; a blocked legitimate command
#   is an expensive one. When in doubt, allow.
#
#   Opt-out:
#     - global:   export MEMTRACE_ENFORCE=off
#     - per-repo: remove/comment its line in the scope list.
#
#   Scope list (the set of indexed repository roots), resolved in order:
#     1. $MEMTRACE_ENFORCE_REPOS               (explicit override)
#     2. $HOME/.memtrace/enforce-repos         (seeded by `memtrace install`,
#                                               refreshed on index; one root
#                                               path per line, # comments
#                                               allowed)
#   No list present -> the hook is inert (allow). This keeps a fresh install
#   silent until a repository has actually been indexed.
#
#   Liveness: the memcore-server gRPC backend (it serves find_code). The HTTP
#   UI port is often down while the backend is fully live, so the process is
#   the reliable signal. Set MEMTRACE_HEALTH_URL to force an HTTP probe
#   instead. Test override: MEMTRACE_ENFORCE_HEALTH=ok|fail bypasses the
#   liveness probe entirely (used by the fixture-driven negative-case tests;
#   never wire this in a live install).
# ---------------------------------------------------------------------------
set -uo pipefail

allow(){ exit 0; }
deny(){
  python3 -c 'import json,sys; print(json.dumps({"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":sys.argv[1]}}))' "$1"
  exit 0
}

# 0. Global opt-out and empty input.
[[ "${MEMTRACE_ENFORCE:-on}" == "off" ]] && allow
input="$(cat 2>/dev/null || true)"; [[ -n "$input" ]] || allow

# 1. Parse the hook payload into: tool name, and two tool-specific fields.
vals="$(printf '%s' "$input" | python3 -c '
import json,sys
try: o=json.load(sys.stdin)
except Exception: o={}
ti=o.get("tool_input",{}) or {}
tn=o.get("tool_name",""); cwd=o.get("cwd","")
print(tn)
if tn=="Grep":
  print(ti.get("pattern","") or ""); print(ti.get("path","") or cwd)
elif tn=="Bash":
  print(ti.get("command","") or ""); print(cwd)
else:
  print(""); print("")
' 2>/dev/null || true)"
TOOL="$(sed -n 1p <<<"$vals")"
A1="$(sed -n 2p <<<"$vals")"
A2="$(sed -n 3p <<<"$vals")"

# 2. Scope: is a path under one of the indexed repository roots?
LIST="${MEMTRACE_ENFORCE_REPOS:-$HOME/.memtrace/enforce-repos}"
in_scope(){
  local p="$1" line root; [[ -n "$p" && -f "$LIST" ]] || return 1
  while IFS= read -r line || [[ -n "$line" ]]; do
    root="${line%%#*}"
    root="${root#"${root%%[![:space:]]*}"}"; root="${root%"${root##*[![:space:]]}"}"; root="${root%/}"
    [[ -z "$root" ]] && continue
    case "$p/" in "$root"/*) return 0 ;; esac
  done < "$LIST"
  return 1
}

# 3. Liveness (last, only for a real candidate). Fail-open when unreachable.
daemon_ok(){
  local h="${MEMTRACE_ENFORCE_HEALTH:-}"          # test override: ok|fail
  [[ -n "$h" ]] && { [[ "$h" == ok ]]; return; }
  if [[ -n "${MEMTRACE_HEALTH_URL:-}" ]]; then
    curl -sf --max-time 1 "$MEMTRACE_HEALTH_URL" >/dev/null 2>&1
  else
    pgrep -f memcore-server >/dev/null 2>&1
  fi
}

case "$TOOL" in
  Grep)
    # npm / Windows builds still expose the Grep tool. Redirect content search
    # on an explicit SOURCE FILE; leave config/data/docs targets alone. A no-path
    # or directory target is ambiguous and allowed (fail-open, below). Redirecting
    # an explicit in-scope source FILE is INTENTIONAL: it is a code-content lookup
    # that find_code/find_symbol answers with callers and context, distinct from
    # Bash `grep foo file` stream filtering (always allowed). Opt-out per repo or
    # MEMTRACE_ENFORCE=off if a literal single-file grep is wanted.
    PATTERN="$A1"; TPATH="$A2"
    shopt -s nocasematch
    case "$TPATH" in
      *.env|*.env.*|*package.json|*.json|*.yaml|*.yml|*.toml|*.ini|*.cfg|*.conf|*.lock|*.md|*.txt|*.csv|*.tsv|*.xml|*readme*|*license*)
        shopt -u nocasematch; allow ;;
    esac
    shopt -u nocasematch
    # A target whose basename has NO file extension (a directory, or an omitted
    # Grep path that defaulted to the repo root) is ambiguous between code and
    # docs/config -> allow (design-contract fail-open: when in doubt, allow).
    # Only an explicit FILE path proceeds to the source-vs-docs redirect.
    case "${TPATH##*/}" in
      *.*) : ;;
      *) allow ;;
    esac
    [[ -n "$TPATH" && -n "$PATTERN" ]] || allow
    in_scope "$TPATH" || allow
    daemon_ok || allow
    deny "Memtrace has indexed this repository. Prefer mcp__memtrace__find_code(query=\"$PATTERN\") or mcp__memtrace__find_symbol over Grep: same match, plus callers, cross-repo edges, and decision context. Config, data, docs, and non-indexed paths are unaffected. Opt-out: MEMTRACE_ENFORCE=off."
    ;;

  Bash)
    # Native builds (Claude Code 2.1.116+) run ugrep/bfs THROUGH Bash. Classify
    # the command: 1 = a standalone recursive code search, 0 = anything else.
    #
    # Recognized as code search (unless a real stdin pipe feeds it, or it
    # targets a data/doc path): rg / ripgrep / ug / ugrep / ag / ack / sift /
    # pt; grep / egrep / fgrep carrying -r/-R/--recursive/--include/
    # --exclude-dir; git grep. Best-effort de-obfuscation strips leading VAR=
    # assignments and the env / command / builtin / exec / nohup / time /
    # xargs / eval wrappers, a backslash-escape, and an absolute path, and it
    # also scans commands inside $(...) and backtick substitutions. This
    # widening is intentionally lossy: anything it cannot resolve falls
    # through to ALLOW.
    CMD="$A1"; CWD="$A2"; [[ -n "$CMD" ]] || allow
    cls="$(MTE_CMD="$CMD" python3 -c '
import os,re,shlex
cmd=os.environ.get("MTE_CMD","")
SEARCH=("rg","ripgrep","ug","ugrep","ag","ack","sift","pt")
PATHDATA=re.compile(r"\.(log|json|ya?ml|toml|ini|cfg|conf|lock|md|txt|csv|tsv|xml|html?)$|(?:^|/)(?:docs?|node_modules|dist|build|target)(?:/|$)|(?:^|/)(?i:readme|license|changelog|contributing)(?:\.[a-z0-9]+)?$|/var/log|/\.git(?:/|$)")
def unwrap(toks):
    ci=0; g=0
    while ci<len(toks) and g<8:
        g+=1; t=toks[ci]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=",t): ci+=1; continue
        if t in ("command","builtin","exec","nohup","time","stdbuf","nice","setsid"): ci+=1; continue
        if t=="env":
            ci+=1
            while ci<len(toks) and (toks[ci].startswith("-") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=",toks[ci])): ci+=1
            continue
        break
    return toks[ci:]
def is_search(base,args):
    base=base.lstrip("\\").rsplit("/",1)[-1]
    if base in SEARCH: return "std"
    if base in ("grep","egrep","fgrep"):
        for a in args:
            if a=="--recursive" or re.match(r"^-[A-Za-z]*[rR]",a) or a.startswith("--include") or a.startswith("--exclude-dir") or a=="--include-dir":
                return "grep"
        return None
    if base=="git" and args and args[0]=="grep": return "git"
    return None
def seg_flag(seg,piped,depth=0):
    if depth>2: return None
    seg=seg.strip()
    if not seg: return None
    try: toks=shlex.split(seg)
    except Exception: return None
    toks=unwrap(toks)
    if not toks: return None
    base=toks[0].lstrip("\\").rsplit("/",1)[-1]; rest=toks[1:]
    if base=="xargs":
        j=0
        while j<len(rest) and rest[j].startswith("-"):
            if rest[j] in ("-I","-i","-n","-P","-d","-E","-s","-L") and j+1<len(rest): j+=2
            else: j+=1
        if j<len(rest) and is_search(rest[j],rest[j+1:]): return seg
        return None
    if base=="eval":
        return seg if seg_flag(" ".join(rest),False,depth+1) else None
    kind=is_search(base,rest)
    if kind is None: return None
    if kind=="std" and piped: return None
    return seg
parts=re.split(r"(\|\||&&|\||;|&)",cmd)
segs=[]
if parts:
    segs.append((parts[0],False))
    i=1
    while i<len(parts):
        op=parts[i]; s=parts[i+1] if i+1<len(parts) else ""
        segs.append((s, op=="|")); i+=2
for m in re.findall(r"\$\(([^()]*)\)",cmd): segs.append((m,False))
for m in re.findall(r"`([^`]*)`",cmd): segs.append((m,False))
hit=None
for s,p in segs:
    h=seg_flag(s,p)
    if h: hit=h; break
if hit is None:
    print("0")
else:
    try: toks=unwrap(shlex.split(hit))
    except Exception: toks=hit.split()
    args=toks[1:] if toks else []
    VALOPT=("-e","--regexp","-f","--file","--include","--exclude","--exclude-dir","-g","--glob","-m","--max-count","-A","-B","-C","--context")
    paths=[]; seen_pat=False; i=0
    while i<len(args):
        a=args[i]
        if a.startswith("-"):
            i+= 2 if (a in VALOPT and i+1<len(args)) else 1
            continue
        if not seen_pat:
            seen_pat=True; i+=1; continue   # the search PATTERN, never a path
        paths.append(a); i+=1
    # every explicit path arg is a docs/data target -> allow; a code path, or a
    # repo-wide search with no path, -> redirect. The pattern is never counted as
    # a path, so an unquoted pattern like `rg docs src/` still redirects.
    if paths and all(PATHDATA.search(pp) for pp in paths): print("0")
    else: print("1")
' 2>/dev/null || echo 0)"
    [[ "$cls" == "1" ]] || allow
    in_scope "$CWD" || allow
    daemon_ok || allow
    deny "Memtrace has indexed this repository. Prefer mcp__memtrace__find_code / mcp__memtrace__find_symbol over recursive code search (rg / ugrep / grep -r / git grep): same match, plus callers, cross-repo edges, and decision context. Plain or piped grep, logs, config, and non-indexed paths are unaffected. Opt-out: MEMTRACE_ENFORCE=off."
    ;;

  *) allow ;;
esac
allow
