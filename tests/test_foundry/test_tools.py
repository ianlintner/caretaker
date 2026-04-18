"""Tests for the foundry tool registry."""

from __future__ import annotations

import pytest

from caretaker.foundry.tools import (
    PathViolation,
    ToolContext,
    _check_write_denylist,
    _resolve_inside_workspace,
    build_tool_registry,
)


class TestPathSafety:
    def test_rejects_absolute_path(self, tool_ctx: ToolContext) -> None:
        with pytest.raises(PathViolation):
            _resolve_inside_workspace(tool_ctx, "/etc/passwd")

    def test_rejects_escaping_path(self, tool_ctx: ToolContext) -> None:
        with pytest.raises(PathViolation):
            _resolve_inside_workspace(tool_ctx, "../outside")

    def test_accepts_inside_path(self, tool_ctx: ToolContext) -> None:
        result = _resolve_inside_workspace(tool_ctx, "README.md")
        assert result == (tool_ctx.workspace_root / "README.md").resolve()

    def test_denylist_rejects_matching_glob(self, tool_ctx: ToolContext) -> None:
        with pytest.raises(PathViolation):
            _check_write_denylist(tool_ctx, ".github/workflows/ci.yml")

    def test_denylist_allows_other_paths(self, tool_ctx: ToolContext) -> None:
        _check_write_denylist(tool_ctx, "src/app.py")  # no raise


class TestReadTools:
    @pytest.mark.asyncio
    async def test_read_file_returns_content(self, tool_ctx: ToolContext) -> None:
        reg = build_tool_registry()
        result = await reg["read_file"].handler(tool_ctx, "README.md")
        assert "hello" in result
        assert "read_file:README.md" in result

    @pytest.mark.asyncio
    async def test_read_file_missing(self, tool_ctx: ToolContext) -> None:
        reg = build_tool_registry()
        result = await reg["read_file"].handler(tool_ctx, "nope.md")
        assert "file not found" in result

    @pytest.mark.asyncio
    async def test_list_files_returns_entries(self, tool_ctx: ToolContext) -> None:
        reg = build_tool_registry()
        result = await reg["list_files"].handler(tool_ctx, ".", "*")
        assert "README.md" in result

    @pytest.mark.asyncio
    async def test_list_files_excludes_git_dir(self, tool_ctx: ToolContext) -> None:
        reg = build_tool_registry()
        result = await reg["list_files"].handler(tool_ctx, ".", "**/*")
        # .git/HEAD must not leak through
        assert ".git/" not in result
        assert "HEAD" not in result

    @pytest.mark.asyncio
    async def test_grep_finds_match(self, tool_ctx: ToolContext) -> None:
        reg = build_tool_registry()
        result = await reg["grep"].handler(tool_ctx, "hello", ".")
        assert "README.md" in result
        assert ":1:hello" in result


class TestMutationTools:
    @pytest.mark.asyncio
    async def test_write_file_creates_file(self, tool_ctx: ToolContext) -> None:
        reg = build_tool_registry()
        result = await reg["write_file"].handler(tool_ctx, "new.txt", "hi there")
        assert "OK" in result
        assert (tool_ctx.workspace_root / "new.txt").read_text() == "hi there"
        assert any("write_file new.txt" in m for m in tool_ctx.mutations)

    @pytest.mark.asyncio
    async def test_write_file_denylist_rejects(self, tool_ctx: ToolContext) -> None:
        reg = build_tool_registry()
        result = await reg["write_file"].handler(tool_ctx, ".github/workflows/ci.yml", "content")
        assert "error" in result
        assert "denylist" in result
        assert not (tool_ctx.workspace_root / ".github/workflows/ci.yml").exists()

    @pytest.mark.asyncio
    async def test_run_command_rejects_non_allowlisted(self, tool_ctx: ToolContext) -> None:
        reg = build_tool_registry()
        result = await reg["run_command"].handler(tool_ctx, "curl", ["http://evil"])
        assert "not in the allowlist" in result

    @pytest.mark.asyncio
    async def test_run_command_runs_echo(self, tool_ctx: ToolContext) -> None:
        reg = build_tool_registry()
        result = await reg["run_command"].handler(tool_ctx, "echo", ["ok"])
        assert "exit_code=0" in result
        assert "ok" in result


class TestGitTools:
    @pytest.mark.asyncio
    async def test_git_status_clean(self, tool_ctx: ToolContext) -> None:
        reg = build_tool_registry()
        result = await reg["git_status"].handler(tool_ctx)
        assert "(clean)" in result or "git_status" in result

    @pytest.mark.asyncio
    async def test_git_status_dirty_after_write(self, tool_ctx: ToolContext) -> None:
        (tool_ctx.workspace_root / "dirty.txt").write_text("x\n")
        reg = build_tool_registry()
        result = await reg["git_status"].handler(tool_ctx)
        assert "dirty.txt" in result


class TestApplyPatch:
    @pytest.mark.asyncio
    async def test_apply_patch_denylist_blocks(self, tool_ctx: ToolContext) -> None:
        diff = (
            "--- a/.github/workflows/ci.yml\n"
            "+++ b/.github/workflows/ci.yml\n"
            "@@ -0,0 +1,1 @@\n"
            "+evil: true\n"
        )
        reg = build_tool_registry()
        result = await reg["apply_patch"].handler(tool_ctx, diff)
        assert "error" in result
        assert "denylist" in result
