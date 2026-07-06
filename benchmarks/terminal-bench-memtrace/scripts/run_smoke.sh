#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT"

OVERRIDE_TBENCH_DATASET="${TBENCH_DATASET-}"
OVERRIDE_TBENCH_TRIALS="${TBENCH_TRIALS-}"
OVERRIDE_TBENCH_TASK_LIMIT="${TBENCH_TASK_LIMIT-}"
OVERRIDE_TBENCH_MODEL="${TBENCH_MODEL-}"
OVERRIDE_TBENCH_REASONING_EFFORT="${TBENCH_REASONING_EFFORT-}"
OVERRIDE_MEMTRACE_VERSION="${MEMTRACE_VERSION-}"
OVERRIDE_TBENCH_JOB_NAME="${TBENCH_JOB_NAME-}"
OVERRIDE_TBENCH_CONCURRENCY="${TBENCH_CONCURRENCY-}"
OVERRIDE_RUN_MEMTRACE_INSTALLER="${RUN_MEMTRACE_INSTALLER-}"
OVERRIDE_CREATE_GIT_IF_MISSING="${CREATE_GIT_IF_MISSING-}"
OVERRIDE_TBENCH_AGENT_SETUP_TIMEOUT_MULTIPLIER="${TBENCH_AGENT_SETUP_TIMEOUT_MULTIPLIER-}"
OVERRIDE_TBENCH_AGENT_TIMEOUT_MULTIPLIER="${TBENCH_AGENT_TIMEOUT_MULTIPLIER-}"
OVERRIDE_MEMTRACE_CREDENTIALS_PATH="${MEMTRACE_CREDENTIALS_PATH-}"
OVERRIDE_MEMTRACE_BUNDLE_DIR="${MEMTRACE_BUNDLE_DIR-}"
OVERRIDE_MEMTRACE_SKILLS_DIR="${MEMTRACE_SKILLS_DIR-}"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

[ -n "$OVERRIDE_TBENCH_DATASET" ] && TBENCH_DATASET="$OVERRIDE_TBENCH_DATASET"
[ -n "$OVERRIDE_TBENCH_TRIALS" ] && TBENCH_TRIALS="$OVERRIDE_TBENCH_TRIALS"
[ -n "$OVERRIDE_TBENCH_TASK_LIMIT" ] && TBENCH_TASK_LIMIT="$OVERRIDE_TBENCH_TASK_LIMIT"
[ -n "$OVERRIDE_TBENCH_MODEL" ] && TBENCH_MODEL="$OVERRIDE_TBENCH_MODEL"
[ -n "$OVERRIDE_TBENCH_REASONING_EFFORT" ] && TBENCH_REASONING_EFFORT="$OVERRIDE_TBENCH_REASONING_EFFORT"
[ -n "$OVERRIDE_MEMTRACE_VERSION" ] && MEMTRACE_VERSION="$OVERRIDE_MEMTRACE_VERSION"
[ -n "$OVERRIDE_TBENCH_JOB_NAME" ] && TBENCH_JOB_NAME="$OVERRIDE_TBENCH_JOB_NAME"
[ -n "$OVERRIDE_TBENCH_CONCURRENCY" ] && TBENCH_CONCURRENCY="$OVERRIDE_TBENCH_CONCURRENCY"
[ -n "$OVERRIDE_RUN_MEMTRACE_INSTALLER" ] && RUN_MEMTRACE_INSTALLER="$OVERRIDE_RUN_MEMTRACE_INSTALLER"
[ -n "$OVERRIDE_CREATE_GIT_IF_MISSING" ] && CREATE_GIT_IF_MISSING="$OVERRIDE_CREATE_GIT_IF_MISSING"
[ -n "$OVERRIDE_TBENCH_AGENT_SETUP_TIMEOUT_MULTIPLIER" ] && TBENCH_AGENT_SETUP_TIMEOUT_MULTIPLIER="$OVERRIDE_TBENCH_AGENT_SETUP_TIMEOUT_MULTIPLIER"
[ -n "$OVERRIDE_TBENCH_AGENT_TIMEOUT_MULTIPLIER" ] && TBENCH_AGENT_TIMEOUT_MULTIPLIER="$OVERRIDE_TBENCH_AGENT_TIMEOUT_MULTIPLIER"
[ -n "$OVERRIDE_MEMTRACE_CREDENTIALS_PATH" ] && MEMTRACE_CREDENTIALS_PATH="$OVERRIDE_MEMTRACE_CREDENTIALS_PATH"
[ -n "$OVERRIDE_MEMTRACE_BUNDLE_DIR" ] && MEMTRACE_BUNDLE_DIR="$OVERRIDE_MEMTRACE_BUNDLE_DIR"
[ -n "$OVERRIDE_MEMTRACE_SKILLS_DIR" ] && MEMTRACE_SKILLS_DIR="$OVERRIDE_MEMTRACE_SKILLS_DIR"

export PATH="/Applications/OrbStack.app/Contents/MacOS/xbin:${HOME}/.local/bin:${PATH}"

DATASET="${TBENCH_DATASET:-terminal-bench/terminal-bench-2-1}"
TRIALS="${TBENCH_TRIALS:-1}"
TASK_LIMIT="${TBENCH_TASK_LIMIT:-10}"
MODEL="${TBENCH_MODEL:-gpt-5.5}"
REASONING_EFFORT="${TBENCH_REASONING_EFFORT:-xhigh}"
MEMTRACE_VERSION="${MEMTRACE_VERSION:-0.6.30}"
MEMTRACE_CREDENTIALS_PATH="${MEMTRACE_CREDENTIALS_PATH:-${HOME}/.config/memtrace/credentials.json}"

: "${OPENAI_API_KEY:?OPENAI_API_KEY must be set in .env or the environment}"
if [ ! -s "$MEMTRACE_CREDENTIALS_PATH" ] && [ -z "${MEMTRACE_LICENSE_KEY:-}" ]; then
  printf 'MEMTRACE_CREDENTIALS_PATH (%s) must exist or MEMTRACE_LICENSE_KEY must be set.\n' \
    "$MEMTRACE_CREDENTIALS_PATH" >&2
  exit 1
fi

DEFAULT_MEMTRACE_BUNDLE_DIR="$(cd "$ROOT/../../.." && pwd -P)/.bench-memtrace-noavx2"
DEFAULT_MEMTRACE_SKILLS_DIR="${HOME}/.codex/plugins/cache/memtrace/memtrace-skills/0.2.0/skills"
if [ -z "${MEMTRACE_BUNDLE_DIR:-}" ] && [ -d "$DEFAULT_MEMTRACE_BUNDLE_DIR" ]; then
  MEMTRACE_BUNDLE_DIR="$DEFAULT_MEMTRACE_BUNDLE_DIR"
fi
if [ -z "${MEMTRACE_SKILLS_DIR:-}" ]; then
  if [ -d "$DEFAULT_MEMTRACE_SKILLS_DIR" ]; then
    MEMTRACE_SKILLS_DIR="$DEFAULT_MEMTRACE_SKILLS_DIR"
  else
    MEMTRACE_SKILLS_DIR="${HOME}/.agents/skills"
  fi
fi
JOB_NAME="${TBENCH_JOB_NAME:-codex-memtrace-${TASK_LIMIT}x${TRIALS}-$(date -u +%Y%m%dT%H%M%SZ)}"

if [ "${1-}" = "--dry-run" ]; then
  printf 'dataset=%s\n' "$DATASET"
  printf 'trials=%s\n' "$TRIALS"
  printf 'task_limit=%s\n' "$TASK_LIMIT"
  printf 'model=%s\n' "$MODEL"
  printf 'reasoning_effort=%s\n' "$REASONING_EFFORT"
  printf 'memtrace_version=%s\n' "$MEMTRACE_VERSION"
  printf 'memtrace_credentials_path=%s\n' "$MEMTRACE_CREDENTIALS_PATH"
  printf 'memtrace_bundle_dir=%s\n' "${MEMTRACE_BUNDLE_DIR:-}"
  printf 'memtrace_skills_dir=%s\n' "${MEMTRACE_SKILLS_DIR:-}"
  printf 'job_name=%s\n' "$JOB_NAME"
  printf 'concurrency=%s\n' "${TBENCH_CONCURRENCY:-1}"
  exit 0
fi

harbor run \
  -d "$DATASET" \
  --agent-import-path agent:CodexMemtraceAgent \
  -m "$MODEL" \
  -k "$TRIALS" \
  -l "$TASK_LIMIT" \
  -n "${TBENCH_CONCURRENCY:-1}" \
  --env docker \
  --job-name "$JOB_NAME" \
  --jobs-dir runs \
  --agent-setup-timeout-multiplier "${TBENCH_AGENT_SETUP_TIMEOUT_MULTIPLIER:-3}" \
  --agent-timeout-multiplier "${TBENCH_AGENT_TIMEOUT_MULTIPLIER:-1}" \
  --agent-kwarg "reasoning_effort=${REASONING_EFFORT}" \
  --agent-kwarg "memtrace_version=${MEMTRACE_VERSION}" \
  --agent-kwarg "memtrace_credentials_path=${MEMTRACE_CREDENTIALS_PATH}" \
  --agent-kwarg "memtrace_bundle_dir=${MEMTRACE_BUNDLE_DIR:-}" \
  --agent-kwarg "memtrace_skills_dir=${MEMTRACE_SKILLS_DIR:-}" \
  --agent-kwarg "run_memtrace_installer=${RUN_MEMTRACE_INSTALLER:-true}" \
  --agent-kwarg "create_git_if_missing=${CREATE_GIT_IF_MISSING:-true}" \
  --artifact /installed-agent/memtrace-version.txt \
  --artifact /installed-agent/memcore-server-path.txt \
  --artifact /installed-agent/memtrace-bundle-path.txt \
  --artifact /installed-agent/memtrace-skills-install.txt \
  --artifact /installed-agent/memtrace-rail-nudge.txt \
  --artifact /logs/agent/codex-memtrace.txt \
  --artifact /logs/agent/codex-config.toml \
  --artifact /logs/agent/codex-hooks.json \
  --artifact /logs/agent/memtrace-preflight.txt \
  --artifact /logs/agent/memtrace-index.log \
  --artifact /logs/agent/memtrace-status.txt \
  --artifact /logs/agent/memtrace-rail-status.txt \
  --artifact /logs/agent/memtrace-server.log \
  -y

python3 scripts/summarize_harbor.py "runs/${JOB_NAME}"
