import json
import shlex
from pathlib import Path, PurePosixPath
from typing import Any

from harbor.agents.installed.codex import Codex
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.trial.paths import EnvironmentPaths
from harbor.utils.env import parse_bool_env_value


class CodexMemtraceAgent(Codex):
    """Harbor Codex agent with a Memtrace pre-index barrier."""

    _OUTPUT_FILENAME = "codex-memtrace.txt"
    _BENCHMARK_DIRECTIVE = """\
You are running inside a non-interactive benchmark. Complete the task yourself.
Do not ask for permission, confirmation, or clarification; there is no user to answer.
Do not stop with a plan or instructions for the user. Make reasonable assumptions, edit the required files, run relevant checks, and leave the requested final state in the workspace.
Memtrace has already indexed this task and the Memtrace MCP server is configured. When the task involves source code, use the available Memtrace skills/MCP for code discovery and relationships before broad manual searching.
"""
    _REMOTE_MEMTRACE_CONFIG_HOME = PurePosixPath("/tmp/memtrace-config")
    _REMOTE_MEMDB_DATA_DIR = PurePosixPath("/tmp/memtrace-memdb")
    _REMOTE_MEMTRACE_DATA_DIR = PurePosixPath("/tmp/memtrace-state")
    _REMOTE_MEMCORE_SERVER_PATH = PurePosixPath("/tmp/memtrace-memcore-server")
    _REMOTE_MEMTRACE_BUNDLE_DIR = PurePosixPath("/opt/memtrace")
    _REMOTE_MEMTRACE_BIN_DIR = PurePosixPath("/opt/memtrace/linux-x64-noavx2/bin")
    _REMOTE_MCP_WRAPPER = PurePosixPath("/tmp/memtrace-mcp-codex-wrapper")
    _REMOTE_MCP_WRAPPER_JS = PurePosixPath("/tmp/memtrace-mcp-codex-wrapper.js")
    _REMOTE_MEMTRACE_SKILLS_SOURCE_DIR = PurePosixPath("/tmp/memtrace-skills-source")

    def __init__(
        self,
        logs_dir: Path,
        memtrace_version: str | None = "0.6.30",
        memtrace_credentials_path: str | None = None,
        memtrace_bundle_dir: str | None = None,
        memtrace_skills_dir: str | None = None,
        run_memtrace_installer: str | bool = True,
        create_git_if_missing: str | bool = True,
        *args: Any,
        **kwargs: Any,
    ):
        super().__init__(logs_dir, *args, **kwargs)
        self.memtrace_version = memtrace_version
        self.memtrace_credentials_path = memtrace_credentials_path
        self.memtrace_bundle_dir = memtrace_bundle_dir
        self.memtrace_skills_dir = memtrace_skills_dir
        self.run_memtrace_installer = parse_bool_env_value(
            run_memtrace_installer,
            name="run_memtrace_installer",
            default=True,
        )
        self.create_git_if_missing = parse_bool_env_value(
            create_git_if_missing,
            name="create_git_if_missing",
            default=True,
        )

    @staticmethod
    def name() -> str:
        return "codex-memtrace"

    async def install(self, environment: BaseEnvironment) -> None:
        await super().install(environment)

        await self.exec_as_root(
            environment,
            command=(
                "if ldd --version 2>&1 | grep -qi musl || [ -f /etc/alpine-release ]; then"
                "  apk add --no-cache git;"
                " elif command -v apt-get &>/dev/null; then"
                "  apt-get update && apt-get install -y git;"
                " elif command -v yum &>/dev/null; then"
                "  yum install -y git;"
                " else"
                '  echo "Warning: No known package manager found, assuming git is available" >&2;'
                " fi"
            ),
            env={"DEBIAN_FRONTEND": "noninteractive"},
        )

        if bundle_dir := self._resolve_optional_path(
            self.memtrace_bundle_dir or self._get_env("MEMTRACE_BUNDLE_DIR")
        ):
            await self._install_memtrace_bundle(environment, bundle_dir)
            return

        version_spec = f"@{self.memtrace_version}" if self.memtrace_version else "@latest"
        install_command = f"""
set -euo pipefail
if [ -s ~/.nvm/nvm.sh ]; then . ~/.nvm/nvm.sh; fi
npm install -g memtrace{version_spec}
MEMTRACE_PACKAGE_ROOT="$(npm root -g)/memtrace"
MEMTRACE_PLATFORM="$(node -p 'process.platform + "-" + process.arch')"
case "$MEMTRACE_PLATFORM" in
  linux-x64)
    npm install --prefix "$MEMTRACE_PACKAGE_ROOT" --no-save --include=optional @memtrace/linux-x64{version_spec}
    ;;
  linux-arm64)
    npm install --prefix "$MEMTRACE_PACKAGE_ROOT" --no-save --include=optional @memtrace/linux-arm64{version_spec}
    ;;
  *)
    true
    ;;
esac
MEMTRACE_MEMCORE="$(find "$MEMTRACE_PACKAGE_ROOT" -path '*/@memtrace/*/bin/memcore-server' -type f 2>/dev/null | head -n 1)"
if [ -z "$MEMTRACE_MEMCORE" ]; then
  MEMTRACE_MEMCORE="$(find "$(npm root -g)" -path '*/@memtrace/*/bin/memcore-server' -type f 2>/dev/null | head -n 1)"
fi
if [ -z "$MEMTRACE_MEMCORE" ]; then
  echo 'memcore-server not found after Memtrace install' >&2
  exit 1
fi
chmod +x "$MEMTRACE_MEMCORE"
ln -sf "$MEMTRACE_MEMCORE" {self._REMOTE_MEMCORE_SERVER_PATH.as_posix()}
printf '%s\\n' "$MEMTRACE_MEMCORE" | tee /installed-agent/memcore-server-path.txt
memtrace --version | tee /installed-agent/memtrace-version.txt
""".strip()
        if self.run_memtrace_installer:
            install_command += """
if command -v memtrace-skills >/dev/null 2>&1; then
  memtrace-skills install --only codex --global --skip-mcp -y
elif [ -f "$MEMTRACE_PACKAGE_ROOT/installer/dist/index.js" ]; then
  node "$MEMTRACE_PACKAGE_ROOT/installer/dist/index.js" install --only codex --global --skip-mcp -y
else
  echo "Memtrace skills installer not found in $MEMTRACE_PACKAGE_ROOT" >&2
  exit 127
fi | tee /installed-agent/memtrace-skills-install.txt
""".rstrip()
        await self.exec_as_agent(
            environment,
            command=install_command,
            env={
                "CI": "1",
                "MEMTRACE_NO_RTK_PROMPT": "1",
                "MEMTRACE_NO_RTK_INIT": "1",
                "MEMTRACE_INSTALL_YES": "1",
                "MEMTRACE_INSTALL_NO_HOOKS": "1",
                "DEBIAN_FRONTEND": "noninteractive",
            },
        )

        await self.exec_as_root(
            environment,
            command=(
                "for bin in memtrace memtrace-skills; do"
                '  BIN_PATH="$(which "$bin" 2>/dev/null || true)";'
                '  if [ -n "$BIN_PATH" ] && [ "$BIN_PATH" != "/usr/local/bin/$bin" ]; then'
                '    ln -sf "$BIN_PATH" "/usr/local/bin/$bin";'
                "  fi;"
                " done"
            ),
        )

    async def _install_memtrace_bundle(
        self,
        environment: BaseEnvironment,
        bundle_dir: Path,
    ) -> None:
        if not (bundle_dir / "linux-x64-noavx2" / "bin" / "memtrace").exists():
            raise FileNotFoundError(
                f"Memtrace bundle missing linux-x64-noavx2/bin/memtrace: {bundle_dir}"
            )

        remote_bundle_dir = self._REMOTE_MEMTRACE_BUNDLE_DIR.as_posix()
        remote_bin_dir = self._REMOTE_MEMTRACE_BIN_DIR.as_posix()
        await self.exec_as_root(
            environment,
            command=f"rm -rf {shlex.quote(remote_bundle_dir)} && mkdir -p {shlex.quote(remote_bundle_dir)}",
        )
        await environment.upload_dir(bundle_dir, remote_bundle_dir)
        await self.exec_as_root(
            environment,
            command=(
                f"chmod +x {shlex.quote(remote_bin_dir + '/memtrace')}\n"
                f"ln -sf {shlex.quote(remote_bin_dir + '/memtrace')} /usr/local/bin/memtrace\n"
                f"mkdir -p /installed-agent\n"
                f"printf 'embedded\\n' > /installed-agent/memcore-server-path.txt\n"
                f"printf '%s\\n' {shlex.quote(str(bundle_dir))} > /installed-agent/memtrace-bundle-path.txt\n"
                f"memtrace --version | tee /installed-agent/memtrace-version.txt"
            ),
        )

        skills_dir = self._resolve_optional_path(
            self.memtrace_skills_dir or self._get_env("MEMTRACE_SKILLS_DIR")
        )
        if skills_dir:
            await self._install_memtrace_skills_bundle(environment, skills_dir)
        else:
            await self.exec_as_agent(
                environment,
                command=(
                    "mkdir -p /installed-agent\n"
                    "printf 'skills_source=missing\\n' > "
                    "/installed-agent/memtrace-skills-install.txt"
                ),
            )

    async def _install_memtrace_skills_bundle(
        self,
        environment: BaseEnvironment,
        skills_dir: Path,
    ) -> None:
        if not skills_dir.exists():
            raise FileNotFoundError(f"Memtrace skills directory not found: {skills_dir}")

        remote_skills_source = self._REMOTE_MEMTRACE_SKILLS_SOURCE_DIR.as_posix()
        await self.exec_as_root(
            environment,
            command=f"rm -rf {shlex.quote(remote_skills_source)} && mkdir -p {shlex.quote(remote_skills_source)}",
        )
        await environment.upload_dir(skills_dir, remote_skills_source)
        await self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail\n"
                "mkdir -p \"$HOME/.agents/skills\" /installed-agent\n"
                f"cp -R {shlex.quote(remote_skills_source)}/. \"$HOME/.agents/skills/\"\n"
                "printf 'skills_source=copied\\nsource=%s\\n' "
                f"{shlex.quote(str(skills_dir))} > /installed-agent/memtrace-skills-install.txt"
            ),
        )

    def _runtime_memtrace_env(self, task_root: str | None = None) -> dict[str, str]:
        env = {
            "CI": "1",
            "MEMTRACE_HEADLESS": "1",
            "XDG_CONFIG_HOME": self._REMOTE_MEMTRACE_CONFIG_HOME.as_posix(),
            "MEMTRACE_DATA_DIR": self._REMOTE_MEMTRACE_DATA_DIR.as_posix(),
            "MEMTRACE_MEMDB_MODE": "embedded",
            "MEMTRACE_MEMDB_DATA_DIR": self._REMOTE_MEMDB_DATA_DIR.as_posix(),
            "MEMTRACE_NO_RTK_PROMPT": "1",
            "MEMTRACE_NO_RTK_INIT": "1",
            "MEMTRACE_START_FORCE": "1",
            "MEMTRACE_RAIL": "nudge",
            "MEMTRACE_UI_PORT": "3030",
            "MEMTRACE_RAIL_SEARCH_URL": "http://127.0.0.1:3030",
        }
        if not (
            self.memtrace_bundle_dir
            or self._get_env("MEMTRACE_BUNDLE_DIR")
        ):
            env["MEMTRACE_MEMCORE_SERVER_PATH"] = (
                self._REMOTE_MEMCORE_SERVER_PATH.as_posix()
            )
        if task_root:
            env["MEMTRACE_WORKSPACE_ROOT"] = task_root
        if not self._resolve_memtrace_credentials_path() and (
            license_key := self._get_env("MEMTRACE_LICENSE_KEY")
        ):
            env["MEMTRACE_LICENSE_KEY"] = license_key
        return env

    @staticmethod
    def _resolve_optional_path(raw_path: str | None) -> Path | None:
        if not raw_path:
            return None
        return Path(raw_path).expanduser().resolve()

    def _build_register_mcp_servers_command(self, task_root: str) -> str:
        lines: list[str] = [
            "[mcp_servers.memtrace]",
            f"command = {json.dumps(self._REMOTE_MCP_WRAPPER.as_posix())}",
            "startup_timeout_sec = 90",
            "",
            "[mcp_servers.memtrace.env]",
            'CI = "1"',
            'MEMTRACE_HEADLESS = "1"',
            f"XDG_CONFIG_HOME = {json.dumps(self._REMOTE_MEMTRACE_CONFIG_HOME.as_posix())}",
            'MEMTRACE_NO_RTK_PROMPT = "1"',
            'MEMTRACE_NO_RTK_INIT = "1"',
            f"MEMTRACE_WORKSPACE_ROOT = {json.dumps(task_root)}",
            "",
        ]
        for server in self.mcp_servers:
            if server.name == "memtrace":
                continue
            lines.append(f"[mcp_servers.{server.name}]")
            if server.transport == "stdio":
                command = server.command or ""
                args = getattr(server, "args", None) or []
                lines.append(f"command = {json.dumps(command)}")
                if args:
                    lines.append(f"args = {json.dumps(args)}")
            else:
                lines.append(f"url = {json.dumps(server.url)}")
            lines.append("")

        config = "\n".join(lines)
        return (
            f"cat > {self._REMOTE_MCP_WRAPPER_JS.as_posix()} <<'JS'\n"
            "const { spawn } = require('child_process');\n"
            "const child = spawn('bash', ['-lc', "
            "\"if [ -s ~/.nvm/nvm.sh ]; then . ~/.nvm/nvm.sh; fi; exec memtrace mcp\"], "
            "{ env: process.env, stdio: ['pipe', 'pipe', 'pipe'] });\n"
            "process.stdin.pipe(child.stdin);\n"
            "child.stderr.pipe(process.stderr);\n"
            "let buffer = '';\n"
            "child.stdout.on('data', chunk => {\n"
            "  buffer += chunk.toString();\n"
            "  const lines = buffer.split(/\\r?\\n/);\n"
            "  buffer = lines.pop();\n"
            "  for (const line of lines) {\n"
            "    if (!line.trim()) continue;\n"
            "    try {\n"
            "      const message = JSON.parse(line);\n"
            "      const content = message?.result?.content;\n"
            "      if (Array.isArray(content)) {\n"
            "        const assistant = content.filter(item => {\n"
            "          const audience = item?.annotations?.audience || [];\n"
            "          return item?.type === 'text' && audience.includes('assistant');\n"
            "        });\n"
            "        const selected = assistant.length ? assistant : content;\n"
            "        const text = selected\n"
            "          .filter(item => item?.type === 'text')\n"
            "          .map(item => item.text || '')\n"
            "          .join('\\n');\n"
            "        message.result.content = [{ type: 'text', text }];\n"
            "      }\n"
            "      process.stdout.write(`${JSON.stringify(message)}\\n`);\n"
            "    } catch (_err) {\n"
            "      process.stdout.write(`${line}\\n`);\n"
            "    }\n"
            "  }\n"
            "});\n"
            "child.on('exit', (code, signal) => {\n"
            "  if (signal) process.kill(process.pid, signal);\n"
            "  process.exit(code ?? 0);\n"
            "});\n"
            "JS\n"
            f"cat > {self._REMOTE_MCP_WRAPPER.as_posix()} <<'SH'\n"
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "if [ -s ~/.nvm/nvm.sh ]; then . ~/.nvm/nvm.sh; fi\n"
            f"exec node {self._REMOTE_MCP_WRAPPER_JS.as_posix()}\n"
            "SH\n"
            f"chmod +x {self._REMOTE_MCP_WRAPPER.as_posix()}\n"
            "cat >>\"$CODEX_HOME/config.toml\" <<'TOML'\n"
            f"{config}"
            "TOML\n"
            f"mkdir -p {EnvironmentPaths.agent_dir.as_posix()}\n"
            f"cp \"$CODEX_HOME/config.toml\" "
            f"{(EnvironmentPaths.agent_dir / 'codex-config.toml').as_posix()}"
        )

    def _build_register_rail_nudge_command(self) -> str:
        hook = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "memtrace route --hook --host codex --mode nudge",
                            }
                        ],
                    }
                ]
            }
        }
        hook_json = json.dumps(hook, indent=2)
        return (
            f"cat > \"$CODEX_HOME/hooks.json\" <<'JSON'\n{hook_json}\nJSON\n"
            "mkdir -p \"$HOME/.codex\" /installed-agent\n"
            "cp \"$CODEX_HOME/hooks.json\" \"$HOME/.codex/hooks.json\"\n"
            f"cp \"$CODEX_HOME/hooks.json\" "
            f"{(EnvironmentPaths.agent_dir / 'codex-hooks.json').as_posix()}\n"
            "printf 'mode=nudge\\ncommand=memtrace route --hook --host codex --mode nudge\\n"
            "codex_home=%s\\n' \"$CODEX_HOME\" "
            "> /installed-agent/memtrace-rail-nudge.txt\n"
            f"memtrace rail status > "
            f"{(EnvironmentPaths.agent_dir / 'memtrace-rail-status.txt').as_posix()} "
            "2>&1 || true"
        )

    async def _task_root(self, environment: BaseEnvironment) -> str:
        result = await self.exec_as_agent(environment, command="pwd -P")
        stdout = result.stdout or ""
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line:
                return line
        raise RuntimeError("Could not resolve task root with pwd -P")

    def _resolve_memtrace_credentials_path(self) -> Path | None:
        raw_path = (
            self.memtrace_credentials_path
            or self._get_env("MEMTRACE_CREDENTIALS_PATH")
        )
        if not raw_path:
            default_path = Path.home() / ".config" / "memtrace" / "credentials.json"
            return default_path if default_path.exists() else None
        return Path(raw_path).expanduser().resolve()

    async def _install_memtrace_credentials(
        self,
        environment: BaseEnvironment,
        env: dict[str, str],
    ) -> None:
        credentials_path = self._resolve_memtrace_credentials_path()
        if not credentials_path:
            return
        if not credentials_path.exists():
            raise FileNotFoundError(f"Memtrace credentials not found: {credentials_path}")

        remote_config_home = self._REMOTE_MEMTRACE_CONFIG_HOME.as_posix()
        remote_credentials_path = (
            self._REMOTE_MEMTRACE_CONFIG_HOME / "memtrace" / "credentials.json"
        ).as_posix()
        preflight_path = (EnvironmentPaths.agent_dir / "memtrace-preflight.txt").as_posix()

        await self.exec_as_agent(
            environment,
            command=(
                f"mkdir -p {shlex.quote(remote_config_home + '/memtrace')}\n"
                f"printf 'credentials_source=cached\\n' >> {shlex.quote(preflight_path)}"
            ),
            env=env,
        )
        await environment.upload_file(credentials_path, remote_credentials_path)
        if environment.default_user is not None:
            await self.exec_as_root(
                environment,
                command=(
                    f"chown {shlex.quote(environment.default_user)} "
                    f"{shlex.quote(remote_credentials_path)}"
                ),
            )
        await self.exec_as_agent(
            environment,
            command=f"chmod 600 {shlex.quote(remote_credentials_path)}",
            env=env,
        )

    async def _warm_memtrace(self, environment: BaseEnvironment, task_root: str) -> None:
        env = self._runtime_memtrace_env(task_root)
        created_git = "1" if self.create_git_if_missing else "0"
        preflight_path = (EnvironmentPaths.agent_dir / "memtrace-preflight.txt").as_posix()
        await self.exec_as_agent(
            environment,
            command=(
                f"mkdir -p {shlex.quote(EnvironmentPaths.agent_dir.as_posix())}\n"
                f": > {shlex.quote(preflight_path)}"
            ),
            env=env,
        )
        await self._install_memtrace_credentials(environment, env)
        command = f"""
set -euo pipefail
if [ -s ~/.nvm/nvm.sh ]; then . ~/.nvm/nvm.sh; fi
TASK_ROOT={shlex.quote(task_root)}
AGENT_DIR={shlex.quote(EnvironmentPaths.agent_dir.as_posix())}
mkdir -p "$AGENT_DIR" "$MEMTRACE_DATA_DIR"
rm -rf "$MEMTRACE_MEMDB_DATA_DIR"
mkdir -p "$MEMTRACE_MEMDB_DATA_DIR"
printf 'task_root=%s\\nmemdb=%s\\n' "$TASK_ROOT" "$MEMTRACE_MEMDB_DATA_DIR" >> "$AGENT_DIR/memtrace-preflight.txt"
if [ -n "${{MEMTRACE_LICENSE_KEY:-}}" ]; then
  printf 'license_key_present=1\\nlicense_key_length=%s\\n' "${{#MEMTRACE_LICENSE_KEY}}" >> "$AGENT_DIR/memtrace-preflight.txt"
else
  printf 'license_key_present=0\\nlicense_key_length=0\\n' >> "$AGENT_DIR/memtrace-preflight.txt"
fi
memtrace --version >> "$AGENT_DIR/memtrace-preflight.txt" 2>&1
if [ ! -e "$TASK_ROOT/.git" ]; then
  if [ "{created_git}" = "1" ]; then
    git -C "$TASK_ROOT" init -q
    git -C "$TASK_ROOT" config user.email "benchmark@example.invalid"
    git -C "$TASK_ROOT" config user.name "Benchmark Harness"
    printf 'created_ephemeral_git=1\\n' >> "$AGENT_DIR/memtrace-preflight.txt"
  else
    printf 'missing_git_repo=1\\n' >> "$AGENT_DIR/memtrace-preflight.txt"
    exit 3
  fi
fi
memtrace index --clear "$TASK_ROOT" > "$AGENT_DIR/memtrace-index.log" 2>&1
memtrace start --headless --force --workspace "$TASK_ROOT" > "$AGENT_DIR/memtrace-server.log" 2>&1 &
echo $! > "$AGENT_DIR/memtrace.pid"
for _ in $(seq 1 60); do
  if memtrace status > "$AGENT_DIR/memtrace-status.txt" 2>&1; then
    break
  fi
  sleep 1
done
memtrace status >> "$AGENT_DIR/memtrace-status.txt" 2>&1 || true
""".strip()
        await self.exec_as_agent(environment, command=command, env=env)

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        task_root = await self._task_root(environment)
        await self._warm_memtrace(environment, task_root)

        rendered_instruction = self.render_instruction(instruction)
        escaped_instruction = shlex.quote(
            f"{self._BENCHMARK_DIRECTIVE}\n\n{rendered_instruction}"
        )

        if not self.model_name:
            raise ValueError("Model name is required")

        model = self.model_name.split("/")[-1]
        cli_flags = self.build_cli_flags()
        cli_flags_arg = (cli_flags + " ") if cli_flags else ""

        auth_json_path = self._resolve_auth_json_path()
        remote_codex_home = self._REMOTE_CODEX_HOME.as_posix()
        remote_secrets_dir = self._REMOTE_CODEX_SECRETS_DIR.as_posix()
        remote_auth_path = (self._REMOTE_CODEX_SECRETS_DIR / "auth.json").as_posix()

        env: dict[str, str] = {
            "CODEX_HOME": remote_codex_home,
            **self._runtime_memtrace_env(task_root),
        }

        await self.exec_as_agent(
            environment,
            command=(
                f'mkdir -p "$CODEX_HOME" {shlex.quote(remote_secrets_dir)} '
                f"{shlex.quote(EnvironmentPaths.agent_dir.as_posix())}"
            ),
            env=env,
        )

        if auth_json_path:
            self.logger.debug("Codex auth: using auth.json from %s", auth_json_path)
            await environment.upload_file(auth_json_path, remote_auth_path)
            if environment.default_user is not None:
                await self.exec_as_root(
                    environment,
                    command=f"chown {environment.default_user} {remote_auth_path}",
                )
            setup_command = (
                f'ln -sf {shlex.quote(remote_auth_path)} "$CODEX_HOME/auth.json"\n'
            )
        else:
            self.logger.debug("Codex auth: using OPENAI_API_KEY")
            env["OPENAI_API_KEY"] = self._get_env("OPENAI_API_KEY") or ""
            setup_command = (
                f"cat >{shlex.quote(remote_auth_path)} <<EOF\n"
                '{\n  "OPENAI_API_KEY": "${OPENAI_API_KEY}"\n}\nEOF\n'
                f"ln -sf {shlex.quote(remote_auth_path)} "
                '"$CODEX_HOME/auth.json"\n'
            )

        if openai_base_url := self._get_env("OPENAI_BASE_URL"):
            env["OPENAI_BASE_URL"] = openai_base_url
            setup_command += (
                '\ncat >>"$CODEX_HOME/config.toml" <<TOML\n'
                'openai_base_url = "${OPENAI_BASE_URL}"\n'
                "TOML"
            )

        skills_command = self._build_register_skills_command()
        if skills_command:
            setup_command += f"\n{skills_command}"

        setup_command += f"\n{self._build_register_mcp_servers_command(task_root)}"
        setup_command += f"\n{self._build_register_rail_nudge_command()}"

        await self.exec_as_agent(environment, command=setup_command, env=env)

        try:
            await self.exec_as_agent(
                environment,
                command=(
                    "if [ -s ~/.nvm/nvm.sh ]; then . ~/.nvm/nvm.sh; fi; "
                    "codex exec "
                    "--ignore-rules "
                    "--dangerously-bypass-approvals-and-sandbox "
                    "--dangerously-bypass-hook-trust "
                    "--skip-git-repo-check "
                    f"--model {shlex.quote(model)} "
                    "--json "
                    "--enable unified_exec "
                    f"{cli_flags_arg}"
                    "-- "
                    f"{escaped_instruction} "
                    f"2>&1 </dev/null | tee "
                    f"{shlex.quote((EnvironmentPaths.agent_dir / self._OUTPUT_FILENAME).as_posix())}"
                ),
                env=env,
            )
        finally:
            try:
                await self.exec_as_agent(
                    environment,
                    command=(
                        f"mkdir -p {EnvironmentPaths.agent_dir.as_posix()}\n"
                        'if [ -d "$CODEX_HOME/sessions" ]; then\n'
                        f"  rm -rf {(EnvironmentPaths.agent_dir / 'sessions').as_posix()}\n"
                        f'  cp -R "$CODEX_HOME/sessions" '
                        f"{(EnvironmentPaths.agent_dir / 'sessions').as_posix()}\n"
                        "fi"
                    ),
                    env=env,
                )
            except Exception:
                pass
            try:
                await self.exec_as_agent(
                    environment,
                    command=f'rm -rf {shlex.quote(remote_secrets_dir)} "$CODEX_HOME"',
                    env=env,
                )
            except Exception:
                pass
