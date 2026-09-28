"""The headless Claude Code runner - Kestrel's actual brain.

Every Claude call for Kestrel's *own* reasoning (never code supervision - that
is `executors/claude_code.py`, an interactive PTY session) goes through here:
one `claude -p` process per turn, no session reuse. Context is Kestrel's job
(`context.py`, `brain/context_assembly.py`), not the CLI's, so there is
deliberately no `--resume`/long-lived session anywhere in this class - see
docs/design.md §4.5.

CLI flags, verified against the docs at code.claude.com/docs (no local
`claude` binary was run to check this - this project's tests never invoke the
real binary, since every invocation bills Callum's subscription; the
documentation site itself was fetched instead):

- `--print` / `-p`: run headless. The query is a positional argument, not the
  flag's value (`claude -p "query" --output-format json` in the docs'
  examples) - passed last here for exactly that reason.
- `--append-system-prompt <text>`: append to, rather than replace
  (`--system-prompt`), the built-in system prompt. Appending is the right
  choice regardless of which is "more correct": replacing it wholesale would
  drop Claude Code's own tool-use instructions, which the MCP tool calls
  below depend on.
- `--model <alias>`: `haiku` / `sonnet` - resolved by the CLI itself.
- `--output-format json`: one finished JSON object, not `stream-json`'s NDJSON
  event stream. Kestrel wants a single finished answer per turn with nothing
  to reassemble.
- `--tools <patterns...>`: restricts which tools are *available* to Claude at
  all. This is not the same as `--allowedTools`, which only skips the
  confirmation prompt for tools that remain on the menu either way (see the
  CLI reference's "Tool Permission Flags" table: "To restrict which tools are
  available, use `--tools` instead"). `--tools` is therefore what actually
  keeps Bash/Edit/Write/Read off the brain's plate, not `--disallowedTools` -
  the latter is passed too, defensively, since the docs available while
  building this did not spell out `--tools`' exact interaction with
  MCP-namespaced globs.
- `--mcp-config <path>` + `--strict-mcp-config`: load *only* the servers named
  in that file, ignoring any project- or user-scoped `.mcp.json`/
  `~/.claude.json` entries entirely. Passed on every call, even with zero
  servers configured (an empty `mcpServers` object), specifically so a
  project's own `.mcp.json` can never hand the brain a tool it wasn't given
  on purpose.

Uncertain / unverified: the exact field name inside the `--output-format
json` envelope (no live CLI available to inspect one). `_parse` accepts
`result`, `content`, `text`, or a bare JSON string, and treats anything else
as a failed run rather than guessing further - see `BrainError`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger("kestrel.brain.runner")

Model = Literal["haiku", "sonnet"]

# Built-in tools the brain must never see. Bash/Edit/Write/Read/NotebookEdit
# are the ones that touch a filesystem or a shell; WebFetch/WebSearch are
# excluded too - anything the brain needs from the world comes back through a
# kestrel-mcp tool, which is what makes "mediates, never filters" (design
# doc §1) enforceable rather than a house rule.
DISALLOWED_BUILTIN_TOOLS: tuple[str, ...] = (
    "Bash",
    "Edit",
    "Write",
    "Read",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
)


class BrainError(RuntimeError):
    """A run that produced no usable answer: non-zero exit, a timeout, or an
    output shape that didn't parse. Every caller of `BrainRunner.run` must
    treat this as a visible failure - a template fallback, a logged event -
    never as silence. See `EventKind.BRAIN_CALL_FAILED`."""


@dataclass(frozen=True)
class BrainResult:
    text: str
    raw: dict[str, Any]
    model: str


@dataclass(frozen=True)
class MCPServerSpec:
    """One stdio server entry for a `--mcp-config` file - see
    `mcp_server.py` for the one Kestrel actually runs."""

    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": "stdio",
            "command": self.command,
            "args": list(self.args),
        }
        if self.env:
            payload["env"] = self.env
        return payload


@dataclass(frozen=True)
class BrainConfig:
    """Binary path/args, timeout, and the working directory are all
    configurable - see `config.py`'s `KESTREL_BRAIN_*` environment
    variables."""

    binary: str = "claude"
    base_args: tuple[str, ...] = ()
    timeout_seconds: float = 60.0
    # A dedicated, empty directory under the data dir - never a project
    # checkout. The brain has no filesystem tools regardless (see
    # `DISALLOWED_BUILTIN_TOOLS`), but an empty cwd means there is nothing
    # underfoot for a future built-in or a misbehaving MCP tool to find.
    work_dir: Path | None = None


def _write_mcp_config(servers: list[MCPServerSpec], directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"mcp-{uuid.uuid4().hex[:8]}.json"
    payload = {"mcpServers": {s.name: s.to_dict() for s in servers}}
    path.write_text(json.dumps(payload))
    return path


class BrainRunner:
    """One `claude -p` process per call. No session reuse, ever - see the
    module docstring for why."""

    def __init__(self, config: BrainConfig) -> None:
        self._config = config

    async def run(
        self,
        *,
        system_prompt: str,
        user_message: str,
        model: Model,
        allowed_tools: list[str] | None = None,
        mcp_servers: list[MCPServerSpec] | None = None,
        cwd: Path | None = None,
    ) -> BrainResult:
        """Runs one headless turn and returns Claude's final text.

        `allowed_tools` is the `--tools` allowlist - typically
        `["mcp__kestrel__*"]` for the conversation brain, or a handful of
        read-only built-ins (`Read`, `Grep`, `Glob`) for `ask_repo`. Left
        empty (narration's case: a one-shot phrasing call with no MCP
        servers configured either) the `--tools` flag is omitted rather than
        passed with zero values - `--disallowedTools` below still keeps
        Bash/Edit/Write/Read off the table regardless. Raises
        `BrainError` on anything short of a clean, parseable result; callers
        decide what "visible failure" looks like for their situation
        (narration.py falls back to a template; the conversation responder
        surfaces the error as a system message).
        """
        work_dir = cwd or self._config.work_dir
        if work_dir is None:
            raise ValueError(
                "BrainRunner needs a work_dir - never the caller's own project checkout"
            )
        work_dir.mkdir(parents=True, exist_ok=True)

        mcp_config_path = _write_mcp_config(mcp_servers or [], work_dir / ".kestrel-mcp")

        args = [
            self._config.binary,
            *self._config.base_args,
            "--print",
            "--append-system-prompt",
            system_prompt,
            "--model",
            model,
            "--output-format",
            "json",
        ]
        if allowed_tools:
            args += ["--tools", *allowed_tools]
        args += [
            "--disallowedTools",
            *DISALLOWED_BUILTIN_TOOLS,
            "--mcp-config",
            str(mcp_config_path),
            "--strict-mcp-config",
            user_message,
        ]

        logger.debug("brain: launching claude -p (model=%s, cwd=%s)", model, work_dir)
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=work_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise BrainError(f"claude binary not found: {self._config.binary!r}") from exc
        except OSError as exc:
            raise BrainError(f"could not start claude: {exc}") from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self._config.timeout_seconds
            )
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise BrainError(f"brain call timed out after {self._config.timeout_seconds}s") from exc

        if proc.returncode != 0:
            raise BrainError(
                f"claude exited {proc.returncode}: {stderr.decode(errors='replace').strip()[:500]}"
            )

        return self._parse(stdout.decode(errors="replace"), model)

    @staticmethod
    def _parse(stdout: str, model: str) -> BrainResult:
        stdout = stdout.strip()
        if not stdout:
            raise BrainError("claude produced no output")
        try:
            raw = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise BrainError(f"could not parse --output-format json: {exc}") from exc

        if isinstance(raw, str):
            return BrainResult(text=raw.strip(), raw={"result": raw}, model=model)
        if isinstance(raw, dict):
            text = raw.get("result")
            if text is None:
                text = raw.get("content")
            if text is None:
                text = raw.get("text")
            if text is None:
                raise BrainError(f"unrecognised --output-format json shape: {sorted(raw)}")
            return BrainResult(text=str(text).strip(), raw=raw, model=model)
        raise BrainError(f"unrecognised --output-format json shape: {type(raw).__name__}")
