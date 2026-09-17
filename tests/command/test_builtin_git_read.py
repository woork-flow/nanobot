from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.command import builtin as builtin_commands
from nanobot.command.builtin import cmd_git, cmd_read
from nanobot.command.router import CommandContext

OUTSIDE_CONTENT = "TOP-SECRET-BODY-MARKER"


class _FakeBus:
    """Collects background command reports instead of routing them."""

    def __init__(self) -> None:
        self.outbound: list[OutboundMessage] = []

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        self.outbound.append(msg)


def _make_ctx(
    tmp_path,
    raw: str,
    args: str = "",
    *,
    restrict: bool = False,
    project: Path | None = None,
    bus: _FakeBus | None = None,
) -> CommandContext:
    msg = InboundMessage(channel="cli", sender_id="u1", chat_id="direct", content=raw)
    loop = SimpleNamespace(
        workspace=tmp_path,
        restrict_to_workspace=restrict,
        bus=bus if bus is not None else _FakeBus(),
        context=SimpleNamespace(timezone="UTC"),
        workspace_scopes=SimpleNamespace(
            for_turn=lambda **_kwargs: SimpleNamespace(
                project_path=project if project is not None else tmp_path,
            ),
        ),
        schedule_background=_schedule_inline,
    )
    return CommandContext(msg=msg, session=None, key=msg.session_key, raw=raw, args=args, loop=loop)


def _schedule_inline(coro) -> None:
    """Track a background coroutine so tests can await it deterministically."""
    _pending_coros.append(coro)


_pending_coros: list = []


async def _run_git(ctx: CommandContext) -> OutboundMessage:
    """Run /git, drain its background task, and return the final report."""
    del _pending_coros[:]
    ack = await cmd_git(ctx)
    assert "syncing" in ack.content, ack.content
    for coro in _pending_coros:
        await coro
    bus = ctx.loop.bus
    assert bus.outbound, f"expected a background report, got ack {ack.content!r}"
    return bus.outbound[-1]


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


def _new_repo(path: Path, remote: str | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "sync@example.com")
    _git(path, "config", "user.name", "Sync")
    (path / "a.md").write_text("a\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "init")
    if remote:
        _git(path, "remote", "add", "origin", remote)
        _git(path, "push", "-qu", "origin", "main")
    return path


def _new_bare(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(path)],
        check=True,
        capture_output=True,
    )
    return path


def _remote_repo(tmp_path: Path, name: str) -> tuple[Path, Path]:
    """Create a bare remote plus a seeded local clone tracking it."""
    bare = _new_bare(tmp_path / f"{name}-remote.git")
    _new_repo(tmp_path / f"{name}-seed", remote=str(bare))
    local = tmp_path / name
    subprocess.run(["git", "clone", "-q", str(bare), str(local)], check=True, capture_output=True)
    _git(local, "config", "user.email", "sync@example.com")
    _git(local, "config", "user.name", "Sync")
    return bare, local


# --- /read: happy paths -----------------------------------------------------


@pytest.mark.asyncio
async def test_cmd_read_returns_markdown_contents(tmp_path) -> None:
    (tmp_path / "NOTE.md").write_text("# Hello\n\nworld", encoding="utf-8")
    ctx = _make_ctx(tmp_path, "/read NOTE.md", args="NOTE.md")

    out = await cmd_read(ctx)

    assert "# 📄 NOTE.md" in out.content
    assert "# Hello" in out.content
    assert "world" in out.content
    # Markdown-capable channels render the body; plain channels read it as text.
    assert out.metadata.get("render_as") != "text"


@pytest.mark.asyncio
async def test_cmd_read_supports_subdirectories(tmp_path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.MD").write_text("# Guide", encoding="utf-8")
    ctx = _make_ctx(tmp_path, "/read docs/guide.MD", args="docs/guide.MD")

    out = await cmd_read(ctx)

    assert "# 📄 docs/guide.MD" in out.content
    assert "# Guide" in out.content


@pytest.mark.asyncio
async def test_cmd_read_uses_session_project_workspace(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "NOTES.md").write_text("# project notes", encoding="utf-8")
    ctx = _make_ctx(tmp_path, "/read NOTES.md", args="NOTES.md", project=project)

    out = await cmd_read(ctx)

    assert "# project notes" in out.content


@pytest.mark.asyncio
async def test_cmd_read_wraps_nested_code_fences(tmp_path) -> None:
    body = "before\n``````\ninner\n``````\nafter"
    (tmp_path / "FENCE.md").write_text(body, encoding="utf-8")
    ctx = _make_ctx(tmp_path, "/read FENCE.md", args="FENCE.md")

    out = await cmd_read(ctx)

    assert body in out.content
    assert out.content.startswith("# 📄 FENCE.md\n\n```````markdown\n")
    assert out.content.endswith("\n```````")


# --- /read: usage and refusals ---------------------------------------------


@pytest.mark.asyncio
async def test_cmd_read_requires_filename(tmp_path) -> None:
    ctx = _make_ctx(tmp_path, "/read", args="")

    out = await cmd_read(ctx)

    assert "Usage: /read <filename>" in out.content
    assert out.metadata.get("render_as") == "text"


@pytest.mark.asyncio
async def test_cmd_read_rejects_non_markdown(tmp_path) -> None:
    (tmp_path / "data.txt").write_text("plain", encoding="utf-8")
    ctx = _make_ctx(tmp_path, "/read data.txt", args="data.txt")

    out = await cmd_read(ctx)

    assert "not a Markdown file" in out.content


@pytest.mark.asyncio
async def test_cmd_read_missing_file(tmp_path) -> None:
    ctx = _make_ctx(tmp_path, "/read missing.md", args="missing.md")

    out = await cmd_read(ctx)

    assert "File not found: `missing.md`" in out.content


@pytest.mark.asyncio
async def test_cmd_read_rejects_directory(tmp_path) -> None:
    (tmp_path / "sub").mkdir()
    ctx = _make_ctx(tmp_path, "/read sub", args="sub")

    out = await cmd_read(ctx)

    assert "is a directory" in out.content


@pytest.mark.asyncio
async def test_cmd_read_reports_undecodable_file(tmp_path) -> None:
    (tmp_path / "bin.md").write_bytes(b"\x00\xff\xfe binary")
    ctx = _make_ctx(tmp_path, "/read bin.md", args="bin.md")

    out = await cmd_read(ctx)

    assert "not UTF-8 text" in out.content


@pytest.mark.asyncio
async def test_cmd_read_reports_unreadable_file(tmp_path, monkeypatch) -> None:
    (tmp_path / "locked.md").write_text("secret", encoding="utf-8")

    def _deny_open(self, *args, **kwargs):
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(Path, "open", _deny_open)
    ctx = _make_ctx(tmp_path, "/read locked.md", args="locked.md")

    out = await cmd_read(ctx)

    assert "Permission denied: cannot read `locked.md`" in out.content


@pytest.mark.asyncio
async def test_cmd_read_reports_io_error(tmp_path, monkeypatch) -> None:
    (tmp_path / "broken.md").write_text("x", encoding="utf-8")

    def _fail_open(self, *args, **kwargs):
        raise OSError(5, "Input/output error", str(self))

    monkeypatch.setattr(Path, "open", _fail_open)
    ctx = _make_ctx(tmp_path, "/read broken.md", args="broken.md")

    out = await cmd_read(ctx)

    assert "Failed to read `broken.md`" in out.content
    assert "Input/output error" in out.content


@pytest.mark.asyncio
async def test_cmd_read_caps_byte_level_read(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(builtin_commands, "_READ_MAX_BYTES", 12)
    # 12 bytes = 4 CJK chars; the byte cap must not split a multi-byte char.
    (tmp_path / "CJK.md").write_text("中文字符测试", encoding="utf-8")
    ctx = _make_ctx(tmp_path, "/read CJK.md", args="CJK.md")

    out = await cmd_read(ctx)

    assert "中文字符" in out.content
    assert "truncated" in out.content


# --- /read: workspace boundary (enforced regardless of the restriction flag) --


@pytest.mark.parametrize("restrict", [True, False])
@pytest.mark.asyncio
async def test_cmd_read_blocks_escape_when_restriction_flag_varies(tmp_path, restrict) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "secret.md"
    outside.write_text(OUTSIDE_CONTENT, encoding="utf-8")

    for arg in ("/tmp/../secret.md", f"{outside}", "../secret.md"):
        target = arg if arg.startswith("/") else f"{workspace}/{arg}"
        if Path(target).resolve() != outside.resolve():
            continue
        ctx = _make_ctx(workspace, f"/read {arg}", args=arg, restrict=restrict)
        out = await cmd_read(ctx)
        assert "outside the workspace" in out.content, f"restrict={restrict} arg={arg}"
        assert OUTSIDE_CONTENT not in out.content


@pytest.mark.parametrize("restrict", [True, False])
@pytest.mark.asyncio
async def test_cmd_read_blocks_symlink_escape(tmp_path, restrict) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "secret.md"
    outside.write_text(OUTSIDE_CONTENT, encoding="utf-8")
    os.symlink(outside, workspace / "link.md")

    ctx = _make_ctx(workspace, "/read link.md", args="link.md", restrict=restrict)

    out = await cmd_read(ctx)

    assert "outside the workspace" in out.content
    assert OUTSIDE_CONTENT not in out.content


@pytest.mark.asyncio
async def test_cmd_read_never_raises_for_bad_paths(tmp_path) -> None:
    ctx = _make_ctx(tmp_path, "/read \x00evil.md", args="\x00evil.md")

    out = await cmd_read(ctx)

    assert out.content


@pytest.mark.asyncio
async def test_cmd_read_truncates_oversized_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(builtin_commands, "_READ_MAX_CHARS", 100)
    (tmp_path / "BIG.md").write_text("x" * 500, encoding="utf-8")
    ctx = _make_ctx(tmp_path, "/read BIG.md", args="BIG.md")

    out = await cmd_read(ctx)

    assert "truncated" in out.content
    assert "x" * 100 in out.content
    assert "x" * 101 not in out.content


# --- /git: success paths ----------------------------------------------------


@pytest.mark.asyncio
async def test_cmd_git_commits_pulls_and_pushes(tmp_path) -> None:
    _bare, local = _remote_repo(tmp_path, "happy")
    (local / "b.md").write_text("b\n", encoding="utf-8")

    out = await _run_git(_make_ctx(local, "/git"))

    assert "✓ synced" in out.content
    assert "git pull --rebase" in out.content
    assert "git push" in out.content
    assert out.metadata.get("render_as") != "text"
    log = _git(local, "log", "-1", "--pretty=%s").stdout.strip()
    assert log.startswith("nanobot sync @ ")


@pytest.mark.asyncio
async def test_cmd_git_tolerates_no_changes(tmp_path) -> None:
    _bare, local = _remote_repo(tmp_path, "clean")

    out = await _run_git(_make_ctx(local, "/git"))

    assert "✓ synced" in out.content
    assert "already clean" in out.content
    assert _git(local, "log", "--oneline").stdout.count("\n") == 1


@pytest.mark.asyncio
async def test_cmd_git_uses_explicit_message_verbatim(tmp_path) -> None:
    _bare, local = _remote_repo(tmp_path, "message")
    (local / "c.md").write_text("c\n", encoding="utf-8")
    message = "- fix: first line\nsecond line   with spaces"

    out = await _run_git(_make_ctx(local, f"/git {message}", args=message))

    assert "✓ synced" in out.content
    assert _git(local, "log", "-1", "--pretty=%B").stdout.strip() == message


@pytest.mark.asyncio
async def test_cmd_git_acknowledges_immediately_without_blocking(tmp_path) -> None:
    _bare, local = _remote_repo(tmp_path, "ack")
    scheduled: list[object] = []
    ctx = _make_ctx(local, "/git")
    ctx.loop.schedule_background = lambda coro: scheduled.append(coro)

    ack = await cmd_git(ctx)

    assert "syncing" in ack.content
    assert scheduled, "the blocking Git work must run off the inbound consumer"
    for coro in scheduled:
        coro.close()


@pytest.mark.parametrize("timezone", ["UTC", "Asia/Shanghai", "America/New_York"])
@pytest.mark.asyncio
async def test_cmd_git_default_message_uses_runtime_timezone(tmp_path, timezone: str) -> None:
    """The default commit message is timestamped in the configured timezone."""
    _bare, local = _remote_repo(tmp_path, f"tz-{timezone.replace('/', '-')}")
    (local / "tz.md").write_text("tz\n", encoding="utf-8")
    ctx = _make_ctx(local, "/git")
    ctx.loop.context.timezone = timezone

    out = await _run_git(ctx)

    assert "✓ synced" in out.content
    subject = _git(local, "log", "-1", "--pretty=%s").stdout.strip()
    assert subject.startswith("nanobot sync @ ")
    recorded = subject.removeprefix("nanobot sync @ ")
    parsed = datetime.strptime(recorded, "%Y-%m-%d %H:%M:%S")
    # The wall clock must follow the configured zone, not the host-local zone.
    expected = datetime.now(ZoneInfo(timezone)).replace(tzinfo=None)
    assert abs((parsed - expected).total_seconds()) < 120, recorded


@pytest.mark.asyncio
async def test_cmd_git_uses_session_project_workspace(tmp_path) -> None:
    _bare, local = _remote_repo(tmp_path, "scoped")
    (local / "d.md").write_text("d\n", encoding="utf-8")
    ctx = _make_ctx(tmp_path, "/git", project=local)

    out = await _run_git(ctx)

    assert "✓ synced" in out.content
    assert str(local) in out.content
    assert "d.md" in _git(local, "show", "--stat", "--pretty=format:").stdout


# --- /git: failure handling -------------------------------------------------


@pytest.mark.asyncio
async def test_cmd_git_reports_missing_binary(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(builtin_commands.shutil, "which", lambda _name: None)

    out = await cmd_git(_make_ctx(tmp_path, "/git"))

    assert "Git is not installed" in out.content
    assert out.metadata.get("render_as") == "text"


@pytest.mark.asyncio
async def test_cmd_git_reports_workspace_that_is_not_a_repo(tmp_path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    out = await _run_git(_make_ctx(plain, "/git"))

    assert "✗ failed" in out.content
    assert "not a Git repository" in out.content


@pytest.mark.asyncio
async def test_cmd_git_reports_missing_remote(tmp_path) -> None:
    repo = _new_repo(tmp_path / "noremote")

    out = await _run_git(_make_ctx(repo, "/git"))

    assert "✗ failed" in out.content
    assert "git pull --rebase" in out.content
    assert "No usable remote is configured" in out.content


@pytest.mark.asyncio
async def test_cmd_git_commit_failure_stops_before_push(tmp_path) -> None:
    _bare, local = _remote_repo(tmp_path, "hooked")
    hook = local / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'policy rejected this commit' >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    (local / "e.md").write_text("e\n", encoding="utf-8")
    before = _git(local, "rev-list", "--count", "HEAD").stdout.strip()

    out = await _run_git(_make_ctx(local, "/git"))

    assert "✗ failed" in out.content
    assert "git commit" in out.content
    assert "✓ synced" not in out.content
    assert "policy rejected this commit" in out.content
    assert _git(local, "rev-list", "--count", "HEAD").stdout.strip() == before


@pytest.mark.asyncio
async def test_cmd_git_reports_rebase_conflict_and_repo_state(tmp_path) -> None:
    _bare, seed = _remote_repo(tmp_path, "conflict")
    local = tmp_path / "conflict-clone"
    subprocess.run(
        ["git", "clone", "-q", str(_bare), str(local)],
        check=True,
        capture_output=True,
    )
    _git(local, "config", "user.email", "sync@example.com")
    _git(local, "config", "user.name", "Sync")
    (seed / "f.txt").write_text("remote version\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "remote change")
    _git(seed, "push", "-q")
    (local / "f.txt").write_text("local version\n", encoding="utf-8")
    _git(local, "add", "-A")
    _git(local, "commit", "-qm", "local change")

    out = await _run_git(_make_ctx(local, "/git"))

    assert "✗ failed" in out.content
    assert "git pull --rebase" in out.content
    assert "stopped on a conflict" in out.content
    assert "git rebase --continue" in out.content
    assert "git rebase --abort" in out.content
    assert "Current repository state" in out.content
    assert (local / ".git" / "rebase-merge").exists() or (local / ".git" / "rebase-apply").exists()
    _git(local, "rebase", "--abort")


@pytest.mark.asyncio
async def test_cmd_git_reports_unreachable_remote(tmp_path) -> None:
    _bare, local = _remote_repo(tmp_path, "unreachable")
    _git(local, "remote", "set-url", "origin", "ssh://git@127.0.0.1:1/nope.git")
    (local / "g.md").write_text("g\n", encoding="utf-8")

    out = await _run_git(_make_ctx(local, "/git"))

    assert "✗ failed" in out.content
    assert "could not be reached" in out.content
    assert "✓ synced" not in out.content


@pytest.mark.asyncio
async def test_cmd_git_step_timeout_is_reported(tmp_path, monkeypatch) -> None:
    _bare, local = _remote_repo(tmp_path, "timeout")
    monkeypatch.setattr(builtin_commands, "_GIT_STEP_TIMEOUT_S", 1)
    real_run = subprocess.run

    def _hang(argv, *args, **kwargs):
        if list(argv)[:2] == ["git", "push"]:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=1, stderr=b"")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(builtin_commands.subprocess, "run", _hang)

    out = await _run_git(_make_ctx(local, "/git"))

    assert "✗ failed" in out.content
    assert "did not finish within" in out.content
    assert "Nothing was pushed" in out.content


@pytest.mark.asyncio
async def test_cmd_git_redacts_credentials_in_output(tmp_path, monkeypatch) -> None:
    _bare, local = _remote_repo(tmp_path, "redact")
    real_run = subprocess.run

    def _leaky(argv, *args, **kwargs):
        if list(argv)[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(
                argv,
                1,
                "",
                "fatal: unable to access "
                "'https://user:ghp_SECRET123456789@github.com/x/y.git/'",
            )
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(builtin_commands.subprocess, "run", _leaky)

    out = await _run_git(_make_ctx(local, "/git"))

    assert "ghp_SECRET123456789" not in out.content
    assert "user:***" in out.content


@pytest.mark.asyncio
async def test_cmd_git_hides_internal_errors(tmp_path, monkeypatch) -> None:
    def _boom(*_args, **_kwargs):
        raise RuntimeError("internal secret detail")

    monkeypatch.setattr(builtin_commands, "_git_sync", _boom)
    monkeypatch.setattr(builtin_commands.shutil, "which", lambda _name: "/usr/bin/git")

    out = await _run_git(_make_ctx(tmp_path, "/git"))

    assert "✗ failed" in out.content
    assert "internal error" in out.content
    assert "internal secret detail" not in out.content


@pytest.mark.asyncio
async def test_cmd_git_respects_workspace_ignore_rules(tmp_path) -> None:
    _bare, local = _remote_repo(tmp_path, "ignored")
    (local / ".gitignore").write_text("scratch/\n", encoding="utf-8")
    (local / "scratch").mkdir()
    (local / "scratch" / "keep-out.md").write_text("ignored", encoding="utf-8")
    (local / "tracked.md").write_text("tracked\n", encoding="utf-8")

    out = await _run_git(_make_ctx(local, "/git"))

    assert "✓ synced" in out.content
    tracked = _git(local, "ls-files").stdout
    assert "tracked.md" in tracked
    assert "scratch/" not in tracked
    assert "keep-out.md" not in tracked


@pytest.mark.asyncio
async def test_cmd_git_reports_unavailable_workspace(tmp_path) -> None:
    ctx = _make_ctx(tmp_path, "/git")
    ctx.loop.workspace_scopes.for_turn = lambda **_kwargs: (_ for _ in ()).throw(
        RuntimeError("no workspace"),
    )

    out = await cmd_git(ctx)

    assert "Cannot resolve the workspace" in out.content


@pytest.mark.asyncio
async def test_cmd_read_reports_unavailable_workspace(tmp_path) -> None:
    ctx = _make_ctx(tmp_path, "/read NOTE.md", args="NOTE.md")
    ctx.loop.workspace_scopes.for_turn = lambda **_kwargs: (_ for _ in ()).throw(
        RuntimeError("no workspace"),
    )

    out = await cmd_read(ctx)

    assert "Cannot resolve the workspace" in out.content


@pytest.mark.asyncio
async def test_cmd_read_uses_real_websocket_workspace_scope(tmp_path) -> None:
    """A WebUI chat bound to its own project reads that project's files."""
    from nanobot.security.workspace_access import (
        WORKSPACE_SCOPE_METADATA_KEY,
        WorkspaceScopeResolver,
    )

    default_ws = tmp_path / "agent-workspace"
    default_ws.mkdir()
    project = tmp_path / "selected-project"
    project.mkdir()
    (project / "PROJECT.md").write_text("# selected project", encoding="utf-8")
    (default_ws / "PROJECT.md").write_text("# default workspace", encoding="utf-8")

    msg = InboundMessage(
        channel="websocket",
        sender_id="u1",
        chat_id="chat",
        content="/read PROJECT.md",
        metadata={WORKSPACE_SCOPE_METADATA_KEY: {"project_path": str(project), "access_mode": "full"}},
    )

    class _NoSessionCreation:
        def get_cached(self, _key):
            return None

        def get_or_create(self, _key):  # pragma: no cover - must not be called
            raise AssertionError("read-only commands must not create a session")

    loop = SimpleNamespace(
        workspace=default_ws,
        bus=_FakeBus(),
        context=SimpleNamespace(timezone="UTC"),
        sessions=_NoSessionCreation(),
        workspace_scopes=WorkspaceScopeResolver(
            default_workspace=default_ws,
            default_restrict_to_workspace=False,
        ),
    )
    ctx = CommandContext(
        msg=msg, session=None, key=msg.session_key, raw="/read PROJECT.md", args="PROJECT.md", loop=loop,
    )

    out = await cmd_read(ctx)

    assert "# selected project" in out.content
    assert "# default workspace" not in out.content


@pytest.mark.asyncio
async def test_cmd_read_falls_back_to_default_workspace_without_scope(tmp_path) -> None:
    """A channel without a project scope uses the configured default workspace."""
    from nanobot.security.workspace_access import WorkspaceScopeResolver

    default_ws = tmp_path / "agent-workspace"
    default_ws.mkdir()
    (default_ws / "HOME.md").write_text("# default workspace", encoding="utf-8")
    msg = InboundMessage(
        channel="telegram", sender_id="u1", chat_id="c", content="/read HOME.md",
    )
    loop = SimpleNamespace(
        workspace=default_ws,
        bus=_FakeBus(),
        context=SimpleNamespace(timezone="UTC"),
        sessions=None,
        workspace_scopes=WorkspaceScopeResolver(
            default_workspace=default_ws,
            default_restrict_to_workspace=False,
        ),
    )
    ctx = CommandContext(
        msg=msg, session=None, key=msg.session_key, raw="/read HOME.md", args="HOME.md", loop=loop,
    )

    out = await cmd_read(ctx)

    assert "# default workspace" in out.content


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_available_for_sync_tests() -> None:
    assert shutil.which("git")
