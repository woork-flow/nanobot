"""Built-in slash command handlers."""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast
from zoneinfo import ZoneInfo

from loguru import logger

from nanobot import __version__
from nanobot.bus.events import INBOUND_META_USER_SHELL, InboundMessage, OutboundMessage
from nanobot.command.router import CommandContext, CommandRouter, normalize_command_text
from nanobot.providers.base import LLMUsage
from nanobot.session.history_visibility import HIDDEN_HISTORY_META
from nanobot.utils.cancellation import shield_and_drain
from nanobot.utils.helpers import build_status_content
from nanobot.utils.restart import set_restart_notice_to_env
from nanobot.utils.workspace_prompts import initialize_workspace_prompt

if TYPE_CHECKING:
    from nanobot.agent.loop import AgentLoop
    from nanobot.session.manager import Session
    from nanobot.utils.gitstore import CommitInfo

# WebUI protocol contract for how a slash command participates in turn state:
# - side_channel: returns control text without starting or ending an agent turn.
# - finalize_active_turn: side-channel command that also closes the active UI turn.
# - stop_active_turn: cancels the active turn; WebUI may intercept exact submits.
# - agent_turn: always enters the normal agent path.
# - agent_turn_with_args: no args is side-channel usage; args enter the agent path.
CommandLifecycle = Literal[
    "side_channel",
    "finalize_active_turn",
    "stop_active_turn",
    "agent_turn",
    "agent_turn_with_args",
]

USER_SHELL_COMMAND = "/__shell"


@dataclass(frozen=True)
class BuiltinCommandSpec:
    command: str
    title: str
    description: str
    icon: str
    arg_hint: str = ""
    lifecycle: CommandLifecycle = "side_channel"
    accepts_args: bool = False

    def as_dict(self) -> dict[str, str | bool]:
        return {
            "command": self.command,
            "title": self.title,
            "description": self.description,
            "icon": self.icon,
            "arg_hint": self.arg_hint,
            "lifecycle": self.lifecycle,
            "accepts_args": self.accepts_args,
        }


BUILTIN_COMMAND_SPECS: tuple[BuiltinCommandSpec, ...] = (
    BuiltinCommandSpec(
        "/new",
        "New chat",
        "Reset this chat and start a fresh conversation.",
        "square-pen",
        lifecycle="finalize_active_turn",
    ),
    BuiltinCommandSpec(
        "/compact",
        "Compact context",
        "Compact this chat's context and continue the conversation.",
        "archive",
    ),
    BuiltinCommandSpec(
        "/stop",
        "Stop current task",
        "Cancel the active agent turn for this chat.",
        "square",
        lifecycle="stop_active_turn",
    ),
    BuiltinCommandSpec(
        "/restart",
        "Restart nanobot",
        "Restart the bot process.",
        "rotate-cw",
    ),
    BuiltinCommandSpec(
        "/status",
        "Show status",
        "Display runtime, provider, and channel status.",
        "activity",
    ),
    BuiltinCommandSpec(
        "/model",
        "Switch model preset",
        "Show or switch the active model preset.",
        "brain",
        "[preset]",
        accepts_args=True,
    ),
    BuiltinCommandSpec(
        "/history",
        "Show conversation history",
        "Print the last N persisted conversation messages.",
        "history",
        "[n]",
        accepts_args=True,
    ),
    BuiltinCommandSpec(
        "/goal",
        "Start long-running goal",
        "Tell the agent to treat the request as a long-running goal.",
        "activity",
        "<goal>",
        lifecycle="agent_turn_with_args",
        accepts_args=True,
    ),
    BuiltinCommandSpec(
        "/trigger",
        "Create named local trigger",
        "Create a named CLI trigger bound to this chat session.",
        "zap",
        "<name>",
        accepts_args=True,
    ),
    BuiltinCommandSpec(
        "/dream",
        "Run Dream",
        "Manually trigger memory consolidation.",
        "sparkles",
    ),
    BuiltinCommandSpec(
        "/dream-log",
        "Show Dream log",
        "Show what the last Dream consolidation changed.",
        "book-open",
        accepts_args=True,
    ),
    BuiltinCommandSpec(
        "/dream-restore",
        "Restore memory",
        "Revert memory to a previous Dream snapshot.",
        "undo-2",
        accepts_args=True,
    ),
    BuiltinCommandSpec(
        "/dream-prompt",
        "Dream memory",
        "Tell Dream how to organize this workspace's memory.",
        "file-text",
        "[init]",
        accepts_args=True,
    ),
    BuiltinCommandSpec(
        "/evaluator-prompt",
        "Heartbeat evaluator",
        "Customize the heartbeat notification gate prompt for this workspace.",
        "file-text",
        "[init]",
        accepts_args=True,
    ),
    BuiltinCommandSpec(
        "/skill",
        "List skills",
        "List all enabled skills available to the agent.",
        "wrench",
    ),
    BuiltinCommandSpec(
        "/git",
        "Git sync",
        "Commit workspace, pull remote, then push to remote.",
        "git-branch",
        "[message]",
        accepts_args=True,
    ),
    BuiltinCommandSpec(
        "/read",
        "Read Markdown file",
        "Print the contents of a Markdown file from the workspace.",
        "file-text",
        "<filename>",
        accepts_args=True,
    ),
    BuiltinCommandSpec(
        "/help",
        "Show help",
        "List available slash commands.",
        "circle-help",
    ),
    BuiltinCommandSpec(
        "/pairing",
        "Manage pairing",
        "List, approve, deny or revoke pairing requests.",
        "shield",
        "[list|approve <code>|deny <code>|revoke <user_id>]",
        accepts_args=True,
    ),
)


def builtin_command_palette() -> list[dict[str, str | bool]]:
    """Return structured command metadata for UI command palettes."""
    return [spec.as_dict() for spec in BUILTIN_COMMAND_SPECS]


def builtin_command_starts_agent_turn(text: str) -> bool:
    """Return whether WebUI ingress should expect a normal agent lifecycle."""
    normalized = normalize_command_text(text)
    command, separator, args = normalized.partition(" ")
    spec = next(
        (item for item in BUILTIN_COMMAND_SPECS if item.command == command.lower()),
        None,
    )
    if spec is None or (separator and not spec.accepts_args):
        return True
    if spec.lifecycle == "agent_turn":
        return True
    return spec.lifecycle == "agent_turn_with_args" and bool(args.strip())


async def cmd_stop(ctx: CommandContext) -> OutboundMessage:
    """Cancel all active tasks and subagents for the session."""
    loop = ctx.loop
    msg = ctx.msg
    total = await loop._cancel_active_tasks(ctx.key)  # pyright: ignore[reportPrivateUsage]
    # Also drain pending queue to prevent mid-turn injection deadlock
    pending = loop._pending_queues.pop(ctx.key, None)  # pyright: ignore[reportPrivateUsage]
    if pending is not None:
        while not pending.empty():
            try:
                pending.get_nowait()
                total += 1
            except Exception:
                break
    content = f"Stopped {total} task(s)." if total else "No active task to stop."
    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id, content=content,
        metadata=dict(msg.metadata or {})
    )


async def cmd_restart(ctx: CommandContext) -> OutboundMessage:
    """Restart the process."""
    msg = ctx.msg
    set_restart_notice_to_env(
        channel=msg.channel,
        chat_id=msg.chat_id,
        metadata=dict(msg.metadata or {}),
    )

    async def _do_restart():
        await asyncio.sleep(1)
        argv = [sys.executable, "-m", "nanobot"] + sys.argv[1:]
        mode = ctx.loop.restart_mode or "auto"
        if mode == "auto":
            mode = "spawn" if sys.platform == "win32" else "exec"
        if mode == "exec":
            os.execv(sys.executable, argv)
            return
        if mode == "spawn":
            kwargs: dict[str, Any] = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            subprocess.Popen(argv, **kwargs)
        os._exit(0)

    asyncio.create_task(_do_restart())
    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id, content="Restarting...",
        metadata=dict(msg.metadata or {})
    )


async def cmd_status(ctx: CommandContext) -> OutboundMessage:
    """Build an outbound status message for a session."""
    loop = ctx.loop
    session = ctx.session or await loop.sessions.state.get(ctx.key)
    runtime = ctx.runtime or await loop.runtime_for_session(session)
    ctx_est = 0
    with suppress(Exception):
        ctx_est, _ = loop.consolidator.estimate_session_prompt_tokens(
            session,
            runtime=runtime,
        )
    last_usage = LLMUsage.from_dict(session.metadata.get("_last_usage"))
    if ctx_est <= 0:
        ctx_est = last_usage.input_tokens if last_usage is not None else 0

    # Await web search provider usage; network requests have a bounded timeout.
    search_usage_text: str | None = None
    # Never let usage fetch break /status
    with suppress(Exception):
        from nanobot.utils.searchusage import fetch_search_usage
        search_cfg = loop.web_config.search
        usage = await fetch_search_usage(
            provider=search_cfg.provider,
            api_key=search_cfg.api_key or None,
        )
        search_usage_text = usage.format()
    active_tasks = loop._active_tasks.get(ctx.key, [])  # pyright: ignore[reportPrivateUsage]
    task_count = sum(1 for t in active_tasks if not t.done())
    with suppress(Exception):
        task_count += loop.subagents.get_running_count_by_session(ctx.key)
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=build_status_content(
            version=__version__, model=runtime.model,
            start_time=loop._start_time, last_usage=last_usage,  # pyright: ignore[reportPrivateUsage]
            context_window_tokens=runtime.context_window_tokens,
            session_msg_count=len(session.get_history(max_messages=0)),
            context_tokens_estimate=ctx_est,
            search_usage_text=search_usage_text,
            active_task_count=task_count,
            max_completion_tokens=runtime.generation.max_tokens,
        ),
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


async def cmd_new(ctx: CommandContext) -> OutboundMessage:
    """Stop active task and start a fresh session."""
    loop = ctx.loop
    await loop._cancel_active_tasks(ctx.key)  # pyright: ignore[reportPrivateUsage]
    loop.discard_session_file_state(ctx.key)
    session = ctx.session or await loop.sessions.state.get(ctx.key)
    snapshot = list(session.messages)
    archive_snapshot = None
    runtime = None
    if session.last_archived < len(snapshot):
        runtime = ctx.runtime or await loop.runtime_for_session(session)
        archive_snapshot = replace(
            session,
            messages=snapshot,
            metadata=dict(session.metadata),
            provider_state=None,
        )
    async def reset_and_schedule_archive() -> None:
        await loop.sessions.state.reset(session.key)
        if archive_snapshot is not None and runtime is not None:
            loop.schedule_background(
                loop.consolidator.archive_session(  # pyright: ignore[reportUnknownMemberType]
                    archive_snapshot,
                    archive_end=len(snapshot),
                    runtime=runtime,
                )
            )

    await shield_and_drain(reset_and_schedule_archive())
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content="New session started.",
        metadata=dict(ctx.msg.metadata or {})
    )


async def cmd_compact(ctx: CommandContext) -> None:
    """Compact the current session without resetting the conversation."""
    loop = ctx.loop
    session = ctx.session or await loop.sessions.state.get(ctx.key)
    runtime = ctx.runtime or await loop.runtime_for_session(session)
    delivery = loop.turn_delivery_factory.create(ctx.msg, ctx.key)

    try:
        await loop.consolidator.compact_idle_session(
            ctx.key,
            runtime=runtime,
            events=delivery.events,
            notify=True,
            reason="manual",
        )
    except Exception:
        logger.exception("Manual context compaction failed for {}", ctx.key)
        return



def _format_preset_names(names: list[str]) -> str:
    return ", ".join(f"`{name}`" for name in names) if names else "(none configured)"


def _model_preset_names(loop: AgentLoop) -> list[str]:
    names = set(loop.model_presets)
    names.add("default")
    return ["default", *sorted(name for name in names if name != "default")]


def _command_error_message(exc: Exception) -> str:
    return str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc)


async def _model_command_status(loop: AgentLoop, session: Session) -> str:
    names = _model_preset_names(loop)
    try:
        runtime = await loop.runtime_for_session(session, recover_removed=False)
    except (KeyError, ValueError) as exc:
        return "\n".join([
            "## Model",
            f"- Current selection error: {_command_error_message(exc)}",
            f"- Available presets: {_format_preset_names(names)}",
            "- Switch with `/model <preset>`.",
        ])
    active = runtime.model_preset or "default"
    return "\n".join([
        "## Model",
        f"- Current model: `{runtime.model}`",
        f"- Current preset: `{active}`",
        f"- Available presets: {_format_preset_names(names)}",
    ])


async def cmd_model(ctx: CommandContext) -> OutboundMessage:
    """Show or switch model presets."""
    loop = ctx.loop
    args = ctx.args.strip()
    metadata = {**dict(ctx.msg.metadata or {}), "render_as": "text"}

    if not args:
        session = ctx.session or await loop.sessions.state.get(ctx.key)
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content=await _model_command_status(loop, session),
            metadata=metadata,
        )

    name = args
    try:
        runtime = await loop.set_session_model_preset(ctx.key, name)
    except (KeyError, ValueError) as exc:
        names = _model_preset_names(loop)
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content=(
                f"Could not switch model preset: {_command_error_message(exc)}\n\n"
                f"Available presets: {_format_preset_names(names)}"
            ),
            metadata=metadata,
        )

    max_tokens = runtime.generation.max_tokens
    lines = [
        f"Switched model preset to `{runtime.model_preset}`.",
        "- Scope: current session",
        f"- Model: `{runtime.model}`",
        f"- Context window: {runtime.context_window_tokens}",
    ]
    lines.append(f"- Max output tokens: {max_tokens}")
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content="\n".join(lines),
        metadata=metadata,
    )


async def cmd_dream(ctx: CommandContext) -> OutboundMessage:
    """Manually trigger a Dream consolidation run."""
    import time

    loop = ctx.loop
    msg = ctx.msg

    if (refusal := _dream_disabled_outbound(ctx)) is not None:
        return refusal

    async def _run_dream():
        from nanobot.agent.memory import MemoryStore

        async def _silent(*_args: Any, **_kwargs: Any) -> None:
            pass

        dream_session_key = MemoryStore.dream_session_key
        build_dream_commit_message = MemoryStore.build_dream_commit_message

        store = loop.context.memory
        content = ""
        resp = None
        diff_body = ""
        t0 = time.monotonic()
        try:
            result = store.build_dream_prompt()
            if result is None:
                await loop.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content=_format_dream_no_input_message(),
                    metadata={"render_as": "text"},
                ))
                return
            prompt, last_cursor = result
            key = dream_session_key()
            dream_runtime = loop.dream_runtime()
            resp = await loop.process_direct(
                prompt,
                session_key=key,
                ephemeral=True,
                tools=store.build_dream_tools(),
                on_progress=_silent,
                runtime=dream_runtime,
            )
            elapsed = time.monotonic() - t0
            # The real file delta grounds the audit record; normal completion
            # decides whether this history batch has finished processing.
            diff_body = store.dream_content_diff()
            completed = MemoryStore.dream_run_completed(resp)
            if completed:
                store.set_last_dream_cursor(last_cursor)
                if diff_body:
                    content = f"Dream completed in {elapsed:.1f}s."
                else:
                    content = f"Dream completed in {elapsed:.1f}s; no memory changes."
            else:
                reason = MemoryStore.dream_incompletion_reason(resp)
                content = (
                    f"Dream did not complete after {elapsed:.1f}s ({reason}); "
                    "memory cursor was not advanced."
                )
        except Exception as e:
            elapsed = time.monotonic() - t0
            content = f"Dream failed after {elapsed:.1f}s: {e}"
        finally:
            if store.git.is_initialized():
                commit_msg = build_dream_commit_message("dream: manual run", diff_body)
                sha = store.git.auto_commit(commit_msg)
                if sha:
                    content += f" (commit {sha})"
            store.compact_history()
            await loop.sessions.state.prune_dream_sessions()
        await loop.bus.publish_outbound(OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=content,
        ))

    asyncio.create_task(_run_dream())
    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id, content="Dreaming...",
    )


async def cmd_dream_prompt(ctx: CommandContext) -> OutboundMessage:
    """Show or set up the workspace Dream memory instructions."""
    # Every Dream form is refused while disabled, including this read-only view
    # and its usage hints, so the disabled surface stays uniform.
    if (refusal := _dream_disabled_outbound(ctx)) is not None:
        return refusal
    store = ctx.loop.context.memory
    path = store.dream_prompt_file
    display_path = path.relative_to(store.workspace).as_posix()
    args = ctx.args.strip().lower()

    if args == "init":
        if not initialize_workspace_prompt(path, store.default_dream_prompt()):
            content = (
                f"Dream memory instructions already exist at `{display_path}`.\n\n"
                "Edit that file, or delete/empty it to return to nanobot's default."
            )
        else:
            content = (
                f"Created Dream memory instructions at `{display_path}`.\n\n"
                "Edit that file to teach Dream how to organize memory. "
                "This fully replaces nanobot's default Dream guide for this workspace. "
                "Delete or empty it to return to nanobot's default."
            )
    elif args:
        content = "Usage: /dream-prompt [init]"
    elif store.has_dream_prompt_override():
        content = (
            "Dream memory instructions: custom for this workspace\n\n"
            f"- Path: `{display_path}`\n"
            "- Delete or empty this file to return to nanobot's default."
        )
    else:
        content = (
            "Dream memory instructions: nanobot default\n\n"
            f"- Editable file: `{display_path}`\n"
            "- Run `/dream-prompt init` to create an editable copy."
        )

    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=content,
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


async def cmd_evaluator_prompt(ctx: CommandContext) -> OutboundMessage:
    """Show or set up the workspace heartbeat evaluator prompt."""
    from nanobot.utils.evaluator import (
        default_evaluator_prompt,
        evaluator_prompt_file,
        has_evaluator_prompt_override,
    )

    workspace = ctx.loop.context.memory.workspace
    path = evaluator_prompt_file(workspace)
    display_path = path.relative_to(workspace).as_posix()
    args = ctx.args.strip().lower()

    if args == "init":
        if not initialize_workspace_prompt(path, default_evaluator_prompt()):
            content = (
                f"Heartbeat evaluator prompt already exists at `{display_path}`.\n\n"
                "Edit that file, or delete/empty it to return to nanobot's default."
            )
        else:
            content = (
                f"Created heartbeat evaluator prompt at `{display_path}`.\n\n"
                "Edit that file to control when the heartbeat notification gate speaks. "
                "It must still instruct the model to call the `evaluate_notification` tool, "
                "otherwise the gate fails closed and stays silent. "
                "Delete or empty it to return to nanobot's default."
            )
    elif args:
        content = "Usage: /evaluator-prompt [init]"
    elif has_evaluator_prompt_override(workspace):
        content = (
            "Heartbeat evaluator prompt: custom for this workspace\n\n"
            f"- Path: `{display_path}`\n"
            "- Delete or empty this file to return to nanobot's default."
        )
    else:
        content = (
            "Heartbeat evaluator prompt: nanobot default\n\n"
            f"- Editable file: `{display_path}`\n"
            "- Run `/evaluator-prompt init` to create an editable copy."
        )

    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=content,
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


def _format_dream_no_input_message() -> str:
    return "\n".join([
        "Dream has no conversation history to process yet.",
        "",
        "Dream reads new entries from `memory/history.jsonl` after the current Dream cursor.",
        (
            "Short chats only reach that file after token compaction or idle auto-compact, "
            "so a fresh or short WebUI chat may leave Dream with no input."
        ),
        "",
        "Next steps:",
        "- Enable `agents.defaults.idleCompactAfterMinutes` so completed chats become Dream input automatically.",
        "- Compact the current chat into memory once that manual action is available.",
        "- If you expected history to exist, check whether `memory/history.jsonl` has new entries after the Dream cursor.",
        "- Use `/dream-prompt` to see or change how Dream organizes memory.",
    ])


def _extract_changed_files(diff: str) -> list[str]:
    """Extract changed file paths from a unified diff."""
    files: list[str] = []
    seen: set[str] = set()
    for line in diff.splitlines():
        if not line.startswith("diff --git "):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        path = parts[3]
        if path.startswith("b/"):
            path = path[2:]
        if path in seen:
            continue
        seen.add(path)
        files.append(path)
    return files


def _format_changed_files(diff: str) -> str:
    files = _extract_changed_files(diff)
    if not files:
        return "No tracked memory files changed."
    return ", ".join(f"`{path}`" for path in files)


_DREAM_COMMIT_PREFIX = "dream:"

# Shared refusal used by every manual Dream command (run, prompt, log,
# restore) so the disabled-config message stays identical across the surface,
# including read-only views such as listing versions.
_DREAM_DISABLED_MESSAGE = (
    "Dream is disabled (`agents.defaults.dream.enabled` is false). "
    "Enable it in config to use Dream commands."
)


def _format_dream_log_content(
    commit: CommitInfo,
    diff: str,
    *,
    requested_sha: str | None = None,
) -> str:
    files_line = _format_changed_files(diff)
    lines = [
        "## Dream Update",
        "",
        "Here is the selected Dream memory change." if requested_sha else "Here is the latest Dream memory change.",
        "",
        f"- Commit: `{commit.sha}`",
        f"- Time: {commit.timestamp}",
        f"- Changed files: {files_line}",
    ]
    if diff:
        lines.extend([
            "",
            f"Use `/dream-restore {commit.sha}` to undo this change.",
            "",
            "```diff",
            diff.rstrip(),
            "```",
        ])
    else:
        lines.extend([
            "",
            "Dream recorded this version, but there is no file diff to display.",
        ])
    return "\n".join(lines)


def _format_dream_restore_list(commits: list[CommitInfo]) -> str:
    lines = [
        "## Dream Restore",
        "",
        "Choose a Dream memory version to restore. Latest first:",
        "",
    ]
    for c in commits:
        lines.append(f"- `{c.sha}` {c.timestamp} - {c.subject()}")
    lines.extend([
        "",
        "Preview a version with `/dream-log <sha>` before restoring it.",
        "Restore a version with `/dream-restore <sha>`.",
    ])
    return "\n".join(lines)


def _dream_disabled_outbound(ctx: CommandContext) -> OutboundMessage | None:
    """Return a refusal message when Dream is disabled at config level, else None."""
    if getattr(ctx.loop, "dream_enabled", True):
        return None
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=_DREAM_DISABLED_MESSAGE,
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


async def cmd_dream_log(ctx: CommandContext) -> OutboundMessage:
    """Show what the last Dream changed.

    Default: diff of the latest Dream commit versus its parent.
    With /dream-log <sha>: diff of that specific commit.
    """
    if (refusal := _dream_disabled_outbound(ctx)) is not None:
        return refusal
    store = ctx.loop.consolidator.store
    git = store.git

    if not git.is_initialized():
        if store.get_last_dream_cursor() == 0:
            msg = (
                "Dream has not run yet. Run `/dream`, or wait for the next scheduled Dream cycle.\n\n"
                "Use `/dream-prompt` to see or change how Dream organizes memory."
            )
        else:
            msg = "Dream history is not available because memory versioning is not initialized."
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=msg, metadata={"render_as": "text"},
        )

    args = ctx.args.strip()

    if args:
        # Show diff of a specific commit
        sha = args.split()[0]
        result = git.show_commit_diff(sha)
        if not result:
            content = (
                f"Couldn't find Dream change `{sha}`.\n\n"
                "Use `/dream-restore` to list recent versions, "
                "or `/dream-log` to inspect the latest one."
            )
        else:
            commit, diff = result
            content = _format_dream_log_content(commit, diff, requested_sha=sha)
    else:
        # Default: show the latest Dream commit's diff
        commits = git.log(max_entries=1, message_prefix=_DREAM_COMMIT_PREFIX)
        result = (
            git.show_commit_diff(
                commits[0].sha,
                max_entries=1,
                message_prefix=_DREAM_COMMIT_PREFIX,
            )
            if commits else None
        )
        if result:
            commit, diff = result
            content = _format_dream_log_content(commit, diff)
        else:
            content = (
                "Dream memory has no saved versions yet.\n\n"
                "Use `/dream-prompt` to see or change how Dream organizes memory."
            )

    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content=content, metadata={"render_as": "text"},
    )


async def cmd_dream_restore(ctx: CommandContext) -> OutboundMessage:
    """Restore memory files from a previous dream commit.

    Usage:
        /dream-restore          — list recent commits
        /dream-restore <sha>    — revert a specific commit
    """
    if (refusal := _dream_disabled_outbound(ctx)) is not None:
        return refusal
    store = ctx.loop.consolidator.store
    git = store.git
    if not git.is_initialized():
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content="Dream history is not available because memory versioning is not initialized.",
        )

    args = ctx.args.strip()
    if not args:
        # Show recent Dream commits for the user to pick
        commits = git.log(max_entries=10, message_prefix=_DREAM_COMMIT_PREFIX)
        if not commits:
            content = "Dream memory has no saved versions to restore yet."
        else:
            content = _format_dream_restore_list(commits)
    else:
        sha = args.split()[0]
        result = git.show_commit_diff(sha, message_prefix=_DREAM_COMMIT_PREFIX)
        if not result:
            content = (
                f"Couldn't restore Dream change `{sha}`.\n\n"
                "Only Dream memory versions can be restored. "
                "Use `/dream-restore` to list recent versions."
            )
        else:
            changed_files = _format_changed_files(result[1])
            new_sha = git.revert(sha, message_prefix=_DREAM_COMMIT_PREFIX)
            if new_sha:
                content = (
                    f"Restored Dream memory to the state before `{sha}`.\n\n"
                    f"- New safety commit: `{new_sha}`\n"
                    f"- Restored files: {changed_files}\n\n"
                    f"Use `/dream-log {new_sha}` to inspect the restore diff."
                )
            else:
                content = (
                    f"Couldn't restore Dream change `{sha}`.\n\n"
                    "It may be the first saved version with no earlier state to restore."
                )
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content=content, metadata={"render_as": "text"},
    )


_HISTORY_DEFAULT_COUNT = 10
_HISTORY_MAX_COUNT = 50
_HISTORY_MAX_CONTENT_CHARS = 200


def _format_history_message(msg: dict[str, Any]) -> str | None:
    """Format a single history message for display. Returns None to skip."""
    role = msg.get("role")
    if role not in ("user", "assistant"):
        return None
    content = msg.get("content") or ""
    if isinstance(content, list):
        parts = [
            text
            for block in cast(list[object], content)
            if (item := cast(dict[str, Any], block) if isinstance(block, dict) else None)
            and item.get("type") == "text"
            and isinstance(text := item.get("text"), str)
        ]
        content = " ".join(parts)
    content = str(content).strip()
    if not content:
        return None
    if len(content) > _HISTORY_MAX_CONTENT_CHARS:
        content = content[:_HISTORY_MAX_CONTENT_CHARS] + "…"
    label = "👤 You" if role == "user" else "🤖 Bot"
    return f"{label}: {content}"


async def cmd_history(ctx: CommandContext) -> OutboundMessage:
    """Show the last N messages of the current session (default 10, max 50).

    Usage: /history [count]
    """
    count = _HISTORY_DEFAULT_COUNT
    if ctx.args.strip():
        try:
            count = max(1, min(int(ctx.args.strip()), _HISTORY_MAX_COUNT))
        except ValueError:
            return OutboundMessage(
                channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
                content="Usage: /history [count] — e.g. /history 5 (default: 10, max: 50)",
                metadata=dict(ctx.msg.metadata or {}),
            )

    session = ctx.session or await ctx.loop.sessions.state.get(ctx.key)
    history = session.get_history(max_messages=0, include_runtime_context=False)
    visible = [_format_history_message(m) for m in history]
    visible = [m for m in visible if m is not None]
    recent = visible[-count:]

    if not recent:
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content="No conversation history yet.",
            metadata=dict(ctx.msg.metadata or {}),
        )

    header = f"Last {len(recent)} message(s):\n"
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content=header + "\n".join(recent),
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


async def cmd_goal(ctx: CommandContext) -> InboundMessage | OutboundMessage:
    """Expand an explicit goal command into model-only input."""
    goal = ctx.args.strip()
    if not goal:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Usage: /goal <long-running task description>",
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )
    if not ctx.is_user_turn:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Goal mode can only be started by a user `/goal <task>` command.",
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )

    session = ctx.session or await ctx.loop.sessions.state.get(ctx.key)
    session.add_message("user", ctx.msg.content, _command=True, media=list(ctx.msg.media))
    await ctx.loop.sessions.state.prepare_input(session)
    return replace(
        ctx.msg,
        content=goal,
        metadata={
            **ctx.msg.metadata,
            "original_command": "/goal",
            "original_content": ctx.msg.content,
            "goal_requested": True,
            "goal_started_at": time.time(),
            HIDDEN_HISTORY_META: {"kind": "goal_request"},
        },
    )


async def cmd_pairing(ctx: CommandContext) -> OutboundMessage:
    """List, approve, deny or revoke pairing requests."""
    from nanobot.pairing import PAIRING_COMMAND_META_KEY, handle_pairing_command
    from nanobot.pairing.events import PairingChanged

    def changed() -> None:
        ctx.loop.bus.publish_nowait(PairingChanged())

    reply = handle_pairing_command(ctx.msg.channel, ctx.args, on_change=changed)
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=reply,
        metadata={PAIRING_COMMAND_META_KEY: True},
    )


async def cmd_skill(ctx: CommandContext) -> OutboundMessage:
    """List all enabled skills (name and description only)."""
    loop = ctx.loop
    skills = loop.context.skills.list_skills(filter_unavailable=False)
    if not skills:
        content = "No skills available."
    else:
        lines = [f"Available skills ({len(skills)}):", ""]
        for entry in skills:
            desc = loop.context.skills.get_skill_description(entry["name"])
            lines.append(f"- **{entry['name']}** — {desc}")
        content = "\n".join(lines)
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=content,
        metadata=dict(ctx.msg.metadata or {}),
    )


async def cmd_trigger(ctx: CommandContext) -> OutboundMessage:
    """Create a local trigger bound to the current session."""
    name = ctx.args.strip()
    if not name:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content=(
                "Usage: /trigger <name>\n\n"
                "Create a named local trigger bound to this chat session."
            ),
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )

    from nanobot.triggers.local_store import LocalTriggerStore

    loop = ctx.loop
    store = loop.local_trigger_store
    if store is None:
        store = LocalTriggerStore(loop.workspace)

    from nanobot.session.keys import UNIFIED_SESSION_KEY

    session_key = (
        ctx.msg.session_key
        if ctx.key == UNIFIED_SESSION_KEY
        else ctx.key
    )
    trigger = store.create(
        name=name,
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        session_key=session_key,
        sender_id="trigger",
        origin_metadata=dict(ctx.msg.metadata or {}),
    )
    command = f'nanobot trigger {trigger.id} "message"'
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=(
            f"Trigger created: {trigger.name}\n"
            f"ID: {trigger.id}\n\n"
            f"Command:\n{command}"
        ),
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )

_WORKSPACE_UNAVAILABLE_MESSAGE = "Cannot resolve the workspace for this session."


def _effective_workspace(ctx: CommandContext) -> Path:
    """Resolve the workspace that applies to this command's session.

    A session that selected its own project (for example a WebUI chat bound to
    a repository) uses that project; every other session falls back to the
    configured default workspace.
    """
    loop = ctx.loop
    resolver = getattr(loop, "workspace_scopes", None)
    if resolver is not None:
        # Same precedence as the turn pipeline: the scope carried by this
        # message wins, then the session's saved scope, then the default
        # workspace. Reading must not create a session as a side effect, so
        # only an already-loaded session contributes its saved scope.
        session_metadata: Any = None
        sessions = getattr(loop, "sessions", None)
        if sessions is not None:
            with suppress(Exception):
                session = ctx.session or sessions.get_cached(ctx.key)
                session_metadata = getattr(session, "metadata", None)
        scope = resolver.for_turn(
            channel=ctx.msg.channel,
            message_metadata=ctx.msg.metadata,
            session_metadata=session_metadata,
        )
        return Path(scope.project_path).expanduser().resolve(strict=False)
    workspace = getattr(loop, "workspace", None)
    if workspace is None:
        workspace = getattr(getattr(loop, "context", None), "workspace", None)
    if workspace is None:
        raise RuntimeError("workspace unavailable for this command")
    return Path(workspace).expanduser().resolve(strict=False)


def _command_outbound(
    ctx: CommandContext,
    content: str,
    *,
    render_as_text: bool = True,
) -> OutboundMessage:
    """Reply to the originating chat, keeping Markdown rendering unless asked off.

    Short control messages set ``render_as: text`` like the other commands.
    Reports meant to be read as Markdown (``/git`` results, ``/read`` bodies)
    keep the channel default so Markdown-capable channels render them and
    plain-text channels still show readable text.
    """
    metadata = dict(ctx.msg.metadata or {})
    if render_as_text:
        metadata["render_as"] = "text"
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=content,
        metadata=metadata,
    )


# --- /git -------------------------------------------------------------------

# Every step is bounded so a credential prompt, an unreachable remote, or a
# hung host-key check cannot keep the workspace sync waiting forever.
_GIT_STEP_TIMEOUT_S = 120
_GIT_EXIT_TIMEOUT = 124

_GIT_NOT_INSTALLED_MESSAGE = "Git is not installed (`git` not found on PATH)."

_GIT_NOT_A_REPO_HINT = (
    "This workspace is not a Git repository, so there is nothing to sync.\n\n"
    "Run `git init` (and add a remote) in the workspace, or point this chat at "
    "a workspace that is already a repository."
)
_GIT_NO_REMOTE_HINT = (
    "No usable remote is configured for this workspace, so the sync stopped "
    "before contacting one.\n\n"
    "Add a remote with `git remote add origin <url>` and make sure the current "
    "branch tracks it, then run `/git` again."
)
_GIT_AUTH_HINT = (
    "The remote rejected the credentials or the access rights, so nothing was "
    "pushed.\n\n"
    "Check that this host can authenticate non-interactively (SSH key, "
    "credential helper, or token) and that it may push to this branch. nanobot "
    "never prompts for credentials, so an interactive login cannot complete here."
)
_GIT_UNREACHABLE_HINT = (
    "The remote could not be reached, so nothing was pushed.\n\n"
    "Check the network path and the remote URL from this host, then run `/git` "
    "again. Your local commit, if any, is still in place."
)
_GIT_PULL_CONFLICT_HINT = (
    "`git pull --rebase` stopped on a conflict, so nothing was pushed.\n\n"
    "The workspace is left mid-rebase with conflict markers in the affected "
    "files. Resolve them, then run `git rebase --continue` — or "
    "`git rebase --abort` to return to the state before this `/git` — and run "
    "`/git` again once the rebase is finished."
)
_GIT_HOST_KEY_HINT = (
    "The SSH connection could not be established without interaction, so "
    "nothing was pushed.\n\n"
    "Accept the host key once from a shell on this host (or add it to "
    "`known_hosts`), then run `/git` again."
)

_GIT_CONFLICT_MARKERS = (
    "could not apply",
    "fix conflicts",
    "conflict",
    "unmerged",
    "rebase in progress",
)
_GIT_NO_REMOTE_MARKERS = (
    "no remote repository specified",
    "there is no tracking information",
    "no configured push destination",
    "does not appear to be a git repository",
)
_GIT_AUTH_MARKERS = (
    "could not read from remote repository",
    "permission denied",
    "authentication failed",
    "invalid username or password",
    "could not read username",
    "terminal prompts disabled",
    "correct access rights",
    " 403",
)
_GIT_UNREACHABLE_MARKERS = (
    "connection refused",
    "could not resolve host",
    "connection timed out",
    "network is unreachable",
)

# Redact credentials that Git can echo back, e.g. remote URLs carrying a token.
_GIT_URL_CREDENTIAL_RE = re.compile(
    r"(?P<scheme>[a-z][a-z0-9+.-]*://)(?P<user>[^/\s:@]+):(?P<secret>[^/\s@]+)@",
    re.IGNORECASE,
)
_GIT_TOKEN_RE = re.compile(
    r"\b(?:gh[pousr]_|github_pat_|glpat-|xox[baprs]-)[A-Za-z0-9_-]{6,}\b",
)


def _redact_git_text(text: str) -> str:
    """Strip credentials and tokens from Git output before it reaches a chat."""
    if not text:
        return text
    redacted = _GIT_URL_CREDENTIAL_RE.sub(
        lambda match: f"{match.group('scheme')}{match.group('user')}:***@",
        text,
    )
    return _GIT_TOKEN_RE.sub("***", redacted)


def _git_env() -> dict[str, str]:
    """Environment for Git steps: non-interactive, bounded, stable messages."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["LC_ALL"] = "C"
    ssh_command = env.get("GIT_SSH_COMMAND") or "ssh"
    if "BatchMode" not in ssh_command:
        ssh_command = f"{ssh_command} -o BatchMode=yes"
    env["GIT_SSH_COMMAND"] = ssh_command
    return env


@dataclass(frozen=True)
class _GitStep:
    """One Git invocation, recorded for the user-facing transcript."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    visible: bool = True

    def echo(self) -> str:
        command = " ".join(shlex.quote(part) for part in self.argv)
        body = "\n".join(
            part for part in (self.stdout.strip(), self.stderr.strip()) if part
        )
        if self.timed_out:
            body = f"{body}\n(timed out after {_GIT_STEP_TIMEOUT_S}s)".strip()
        return f"$ {command}\n{body}".strip() if body else f"$ {command}"

    @property
    def output(self) -> str:
        return f"{self.stdout}\n{self.stderr}".lower()


def _run_git_step(
    argv: list[str],
    workspace: Path,
    env: dict[str, str],
    *,
    visible: bool = True,
) -> _GitStep:
    """Run one Git step with a hard timeout and no chance of a prompt."""
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, workspace-scoped cwd
            argv,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_GIT_STEP_TIMEOUT_S,
            env=env,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        return _GitStep(
            argv=tuple(argv),
            returncode=_GIT_EXIT_TIMEOUT,
            stdout=_redact_git_text(_decode_git_output(exc.stdout)),
            stderr=_redact_git_text(_decode_git_output(exc.stderr)),
            timed_out=True,
            visible=visible,
        )
    except OSError as exc:
        # A missing working directory surfaces here; report it as a step
        # failure instead of blaming the Git binary, which is checked up front.
        return _GitStep(
            argv=tuple(argv),
            returncode=1,
            stderr=exc.strerror or str(exc),
            visible=visible,
        )
    return _GitStep(
        argv=tuple(argv),
        returncode=proc.returncode,
        stdout=_redact_git_text(proc.stdout or ""),
        stderr=_redact_git_text(proc.stderr or ""),
        visible=visible,
    )


def _decode_git_output(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _git_step_hint(step: str, result: _GitStep) -> str:
    """Translate raw Git failures into something a user can act on."""
    output = result.output
    if result.timed_out:
        return (
            f"`{step}` did not finish within {_GIT_STEP_TIMEOUT_S}s and was "
            "cancelled. Nothing was pushed.\n\n"
            "This usually means the remote is unreachable or a prompt was "
            "waiting for input; run the same step from a shell on this host to "
            "see what it needs."
        )
    if "not a git repository" in output:
        return _GIT_NOT_A_REPO_HINT
    if "host key verification failed" in output or "batchmode" in output:
        return _GIT_HOST_KEY_HINT
    if step == "git pull --rebase" and _git_has_conflict(output):
        return _GIT_PULL_CONFLICT_HINT
    # A remote that is configured but unreachable or unauthorized is an access
    # problem; a missing remote or upstream is a configuration problem. Check
    # the configuration markers first so they are not reported as auth failures.
    if any(marker in output for marker in _GIT_NO_REMOTE_MARKERS):
        return _GIT_NO_REMOTE_HINT
    if any(marker in output for marker in _GIT_UNREACHABLE_MARKERS):
        return _GIT_UNREACHABLE_HINT
    if any(marker in output for marker in _GIT_AUTH_MARKERS):
        return _GIT_AUTH_HINT
    return f"`{step}` failed (exit {result.returncode})."


def _git_has_conflict(output: str) -> bool:
    return any(marker in output for marker in _GIT_CONFLICT_MARKERS)


@dataclass(frozen=True)
class _GitSyncOutcome:
    """Result of the fixed stage → commit → pull → push sequence."""

    returncode: int
    steps: tuple[_GitStep, ...]
    failed_step: str | None = None
    hint: str | None = None
    state: str | None = None
    no_changes: bool = False


def _git_sync(workspace: Path, message: str) -> _GitSyncOutcome:
    """Commit the workspace, rebase onto the remote, then push.

    Only "nothing to commit" is tolerated; every other failure stops the
    sequence so a broken commit is never pushed.
    """
    env = _git_env()
    steps: list[_GitStep] = []

    def run(argv: list[str], *, visible: bool = True) -> _GitStep:
        result = _run_git_step(argv, workspace, env, visible=visible)
        steps.append(result)
        return result

    if not workspace.is_dir():
        return _GitSyncOutcome(
            returncode=1,
            steps=tuple(steps),
            hint=(
                f"The workspace `{workspace}` does not exist, so there is "
                "nothing to sync."
            ),
        )
    probe = run(["git", "rev-parse", "--is-inside-work-tree"], visible=False)
    if probe.returncode != 0:
        return _GitSyncOutcome(
            returncode=probe.returncode or 128,
            steps=tuple(steps),
            hint=_GIT_NOT_A_REPO_HINT,
        )

    staged = run(["git", "add", "-A"])
    if staged.returncode != 0:
        return _GitSyncOutcome(
            returncode=staged.returncode,
            steps=tuple(steps),
            failed_step="git add -A",
            hint=_git_step_hint("git add -A", staged),
        )

    # A clean tree is the only non-failure exit from the commit step.
    no_changes = run(["git", "diff", "--cached", "--quiet"], visible=False).returncode == 0
    if not no_changes:
        commit = run(["git", "commit", "-m", message])
        if commit.returncode != 0:
            return _GitSyncOutcome(
                returncode=commit.returncode,
                steps=tuple(steps),
                failed_step="git commit",
                hint=_git_step_hint("git commit", commit),
            )

    pull = run(["git", "pull", "--rebase"])
    if pull.returncode != 0:
        conflicted = _git_has_conflict(pull.output)
        state = None
        if conflicted:
            status = _run_git_step(
                ["git", "status", "--short", "--branch"],
                workspace,
                env,
                visible=False,
            )
            state = status.stdout.strip() or status.stderr.strip() or None
        return _GitSyncOutcome(
            returncode=pull.returncode,
            steps=tuple(steps),
            failed_step="git pull --rebase",
            hint=_git_step_hint("git pull --rebase", pull),
            state=state,
            no_changes=no_changes,
        )

    push = run(["git", "push"])
    if push.returncode != 0:
        return _GitSyncOutcome(
            returncode=push.returncode,
            steps=tuple(steps),
            failed_step="git push",
            hint=_git_step_hint("git push", push),
            no_changes=no_changes,
        )

    return _GitSyncOutcome(returncode=0, steps=tuple(steps), no_changes=no_changes)


def _format_git_sync(workspace: Path, outcome: _GitSyncOutcome) -> str:
    """Render a `/git` result: status line, command transcript, then guidance."""
    if outcome.returncode == 0:
        header = "Git ✓ synced"
    else:
        header = f"Git ✗ failed (exit {outcome.returncode})"
        if outcome.failed_step:
            header += f" at `{outcome.failed_step}`"

    transcript = [step.echo() for step in outcome.steps if step.visible]
    lines = [header, f"Workspace: `{workspace}`", ""]
    if transcript:
        lines += ["```", *transcript, "```"]
    if outcome.no_changes and outcome.returncode == 0:
        lines += ["", "No local changes to commit; the working tree was already clean."]
    if outcome.hint:
        lines += ["", outcome.hint]
    if outcome.state:
        lines += ["", "Current repository state:", "```", outcome.state, "```"]
    return "\n".join(lines)


def _default_git_message(ctx: CommandContext) -> str:
    """Build the timestamped default commit message in the runtime timezone."""
    tz_name = getattr(getattr(ctx.loop, "context", None), "timezone", None)
    try:
        tz = ZoneInfo(tz_name) if tz_name else None
    except Exception:
        tz = None
    now = datetime.now(tz) if tz is not None else datetime.now().astimezone()
    return f"nanobot sync @ {now.strftime('%Y-%m-%d %H:%M:%S')}"


def _schedule_background(ctx: CommandContext, coro: Any) -> None:
    """Run *coro* off the inbound consumer so other chats keep flowing."""
    scheduler = getattr(ctx.loop, "schedule_background", None)
    if scheduler is not None:
        scheduler(coro)
        return
    asyncio.ensure_future(coro)


async def cmd_git(ctx: CommandContext) -> OutboundMessage:
    """Commit the workspace, pull with rebase, then push to the remote.

    Usage: /git [commit message]

    The sequence is fixed: stage everything the workspace's own ignore rules
    allow, commit, ``git pull --rebase``, ``git push``. Without an explicit
    message a timestamped default is generated. The blocking Git calls run in a
    background task so a slow remote never blocks other sessions; the report is
    published back to the originating chat.
    """
    msg = ctx.msg
    metadata = dict(msg.metadata or {})
    if shutil.which("git") is None:
        return _command_outbound(ctx, _GIT_NOT_INSTALLED_MESSAGE)
    try:
        workspace = _effective_workspace(ctx)
    except Exception:
        logger.exception("Cannot resolve workspace for /git")
        return _command_outbound(ctx, _WORKSPACE_UNAVAILABLE_MESSAGE)

    message = ctx.args.strip() or _default_git_message(ctx)
    bus = getattr(ctx.loop, "bus", None)

    async def _run_sync() -> None:
        try:
            outcome = await asyncio.to_thread(_git_sync, workspace, message)
        except FileNotFoundError:
            content = _GIT_NOT_INSTALLED_MESSAGE
        except Exception:
            # Raw internals never reach a chat; the log keeps the details.
            logger.exception("Git sync failed for {}", workspace)
            content = (
                "Git ✗ failed\n\n"
                f"Workspace: `{workspace}`\n\n"
                "The sync could not be completed because of an internal error. "
                "Check the gateway log for details."
            )
        else:
            content = _format_git_sync(workspace, outcome)
        if bus is None:  # pragma: no cover - defensive
            logger.warning("No message bus available to report /git result")
            return
        await bus.publish_outbound(
            OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=content,
                metadata=dict(metadata),
            ),
        )

    _schedule_background(ctx, _run_sync())
    return _command_outbound(
        ctx,
        f"Git syncing `{workspace}`…\n\nCommit → pull --rebase → push; the result "
        "follows in this chat.",
    )


# --- /read ------------------------------------------------------------------

# Bound the echoed body so a huge workspace file cannot flood a chat channel.
# The byte cap keeps the in-memory read bounded too; four bytes per character
# covers the widest UTF-8 sequence, so truncation never loses visible content.
_READ_MAX_CHARS = 64_000
_READ_MAX_BYTES = _READ_MAX_CHARS * 4
_READ_OUTSIDE_HINT = (
    "Only files inside this session's workspace can be read, regardless of the "
    "workspace restriction setting."
)


def _read_text_file(path: Path) -> tuple[str, bool]:
    """Read *path* as bounded UTF-8 text.

    Returns the decoded text and whether reading stopped early at the byte cap.
    Binary or non-UTF-8 content raises ``UnicodeError``.
    """
    with path.open("rb") as handle:
        data = handle.read(_READ_MAX_BYTES + 1)
    capped = len(data) > _READ_MAX_BYTES
    if capped:
        data = data[:_READ_MAX_BYTES]
    if b"\x00" in data[:8192]:
        raise UnicodeError("binary content")
    if not capped:
        return data.decode("utf-8"), False
    # The byte cap can split a multi-byte sequence; drop only the partial tail.
    for trim in range(4):
        if trim:
            data = data[:-1]
        try:
            return data.decode("utf-8"), True
        except UnicodeDecodeError:
            continue
    raise UnicodeError("binary content")


def _markdown_fence(text: str) -> str:
    """Return a backtick fence long enough to wrap *text* verbatim."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _workspace_relative(path: Path, workspace: Path) -> str:
    try:
        return path.relative_to(workspace).as_posix()
    except ValueError:
        return path.name


async def cmd_read(ctx: CommandContext) -> OutboundMessage:
    """Print a Markdown file from the session workspace.

    Usage: /read <filename>

    Paths resolve against the session's workspace and must stay inside it even
    when workspace restriction is disabled; only ``.md`` files are served. The
    command is read-only and always replies with a readable message instead of
    raising into the agent loop.
    """
    from nanobot.security.workspace_policy import resolve_allowed_path

    name = ctx.args.strip()
    if not name:
        return _command_outbound(
            ctx,
            "Usage: /read <filename>\n\nRead a Markdown file from the workspace.",
        )

    try:
        workspace = _effective_workspace(ctx)
    except Exception:
        logger.exception("Cannot resolve workspace for /read")
        return _command_outbound(ctx, _WORKSPACE_UNAVAILABLE_MESSAGE)

    try:
        resolved = resolve_allowed_path(
            name,
            workspace=workspace,
            allowed_root=workspace,
            strict=False,
        )
        display = _workspace_relative(resolved, workspace)
        if resolved.is_dir():
            return _command_outbound(ctx, f"`{display}` is a directory, not a Markdown file.")
        if resolved.suffix.lower() != ".md":
            return _command_outbound(
                ctx,
                f"Refused: `{display}` is not a Markdown file (only `.md` is supported).",
            )
        if not resolved.is_file():
            return _command_outbound(ctx, f"File not found: `{display}`")
        try:
            text, byte_capped = await asyncio.to_thread(_read_text_file, resolved)
        except PermissionError:
            return _command_outbound(ctx, f"Permission denied: cannot read `{display}`.")
        except UnicodeError:
            return _command_outbound(
                ctx,
                f"Cannot read `{display}`: the file is not UTF-8 text "
                "(binary content or another encoding).",
            )
        except OSError as exc:
            return _command_outbound(
                ctx,
                f"Failed to read `{display}`: {exc.strerror or exc.__class__.__name__}.",
            )
    except PermissionError:
        return _command_outbound(
            ctx,
            f"Cannot read `{name}`: it is outside the workspace.\n\n{_READ_OUTSIDE_HINT}",
        )
    except Exception:
        logger.exception("Cannot read {!r} from {}", name, workspace)
        return _command_outbound(ctx, f"Cannot read `{name}`: the path could not be resolved.")

    body = text[:_READ_MAX_CHARS]
    truncated = len(text) > _READ_MAX_CHARS or byte_capped
    fence = _markdown_fence(body)
    content = f"# 📄 {display}\n\n{fence}markdown\n{body}\n{fence}"
    if truncated:
        content += (
            f"\n\n(truncated: showing the first {len(body):,} characters of a larger file)"
        )
    return _command_outbound(ctx, content, render_as_text=False)


async def cmd_help(ctx: CommandContext) -> OutboundMessage:
    """Return available slash commands."""
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=build_help_text(),
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


async def cmd_user_shell(ctx: CommandContext) -> OutboundMessage:
    """Run a trusted local ``!command`` through nanobot's exec policy."""
    metadata = dict(ctx.msg.metadata or {})
    if (
        ctx.msg.channel != "websocket"
        or metadata.get("webui") is not True
        or metadata.get(INBOUND_META_USER_SHELL) is not True
    ):
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Shell commands are only available from a trusted local client.",
            metadata={**metadata, "render_as": "text"},
        )
    if not ctx.args.strip():
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Type a command after `!`, for example `!pwd`.",
            metadata={**metadata, "render_as": "text"},
        )
    return await ctx.loop.execute_user_shell_command(ctx)


def build_help_text() -> str:
    """Build canonical help text shared across channels."""
    lines = ["🐈 nanobot commands:"]
    for spec in BUILTIN_COMMAND_SPECS:
        command = spec.command
        if spec.arg_hint:
            command = f"{command} {spec.arg_hint}"
        lines.append(f"{command} — {spec.description}")
    return "\n".join(lines)


def register_builtin_commands(router: CommandRouter) -> None:
    """Register the default set of slash commands."""
    router.priority("/stop", cmd_stop)
    router.priority("/restart", cmd_restart)
    router.priority("/status", cmd_status)
    router.exact("/new", cmd_new)
    router.exact("/compact", cmd_compact, session_queue=True)
    router.exact("/status", cmd_status)
    router.exact("/model", cmd_model)
    router.prefix("/model ", cmd_model)
    router.exact("/history", cmd_history)
    router.prefix("/history ", cmd_history)
    router.exact("/goal", cmd_goal)
    router.prefix("/goal ", cmd_goal)
    router.exact("/trigger", cmd_trigger)
    router.prefix("/trigger ", cmd_trigger)
    router.exact("/dream", cmd_dream)
    router.exact("/dream-log", cmd_dream_log)
    router.prefix("/dream-log ", cmd_dream_log)
    router.exact("/dream-restore", cmd_dream_restore)
    router.prefix("/dream-restore ", cmd_dream_restore)
    router.exact("/dream-prompt", cmd_dream_prompt)
    router.prefix("/dream-prompt ", cmd_dream_prompt)
    router.exact("/evaluator-prompt", cmd_evaluator_prompt)
    router.prefix("/evaluator-prompt ", cmd_evaluator_prompt)
    router.exact("/skill", cmd_skill)
    router.exact("/git", cmd_git)
    router.prefix("/git ", cmd_git)
    router.exact("/read", cmd_read)
    router.prefix("/read ", cmd_read)
    router.exact("/help", cmd_help)
    router.exact("/pairing", cmd_pairing)
    router.prefix("/pairing ", cmd_pairing)
    router.exact(USER_SHELL_COMMAND, cmd_user_shell)
    router.prefix(f"{USER_SHELL_COMMAND} ", cmd_user_shell)
