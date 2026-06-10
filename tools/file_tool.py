"""
tools/file_tool.py

文件操作工具，提供三个 action：
- file_read:   读取文件全部内容
- file_view:   分窗口查看文件（防止一次读爆上下文）
- file_write:  写入文件（全量覆盖）

设计原则：
- file_read 对大文件做行数截断，超出时提示用 file_view 分页
- file_view 维护"窗口"概念，每次返回固定行数，agent 可 scroll
- file_write 写入前自动创建父目录，写入后返回行数确认
- 所有路径都限制在 repo_path 内（防止读取系统文件）
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from agent.failure import (
    FAILURE_TYPE_PATCH_CONFLICT,
    FAILURE_TYPE_TOOL_FAILURE,
    FAILURE_TYPE_WORKSPACE_ERROR,
)
from agent.patch import Patch
from tools.base import BaseTool, ToolResult


# 单次 file_read 最多返回的行数，超出提示用 file_view
MAX_READ_LINES = 500
# file_view 每窗口显示的行数
VIEW_WINDOW_LINES = 100


class FileReadTool(BaseTool):
    """
    读取文件内容。超过 MAX_READ_LINES 行时截断并提示。

    params:
        path (str): 文件路径（相对或绝对）
    """

    @property
    def name(self) -> str:
        return "file_read"

    @property
    def description(self) -> str:
        return (
            f"Read the contents of a file. "
            f"Files longer than {MAX_READ_LINES} lines will be truncated; "
            f"use file_view with line numbers to read specific sections."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to read (absolute or relative to repo root)",
                },
            },
            "required": ["path"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        path = Path(params.get("path", ""))
        if not path.exists():
            return ToolResult(
                success=False,
                output="",
                error=f"File not found: {path}",
            )
        if not path.is_file():
            return ToolResult(
                success=False,
                output="",
                error=f"Not a file: {path}",
            )

        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as e:
            return ToolResult(success=False, output="", error=str(e), failure_type=FAILURE_TYPE_WORKSPACE_ERROR)

        total = len(lines)
        truncated = total > MAX_READ_LINES
        display_lines = lines[:MAX_READ_LINES]

        # 加行号，方便 agent 用 file_view 定位
        numbered = "\n".join(
            f"{i + 1:4d} | {line}"
            for i, line in enumerate(display_lines)
        )

        suffix = ""
        if truncated:
            suffix = (
                f"\n... ({total - MAX_READ_LINES} more lines not shown) "
                f"Use file_view with start_line to read the rest."
            )

        return ToolResult(
            success=True,
            output=f"File: {path} ({total} lines total)\n{numbered}{suffix}",
        )


class FileViewTool(BaseTool):
    """
    分窗口查看文件，每次返回 VIEW_WINDOW_LINES 行。

    params:
        path (str):       文件路径
        start_line (int): 从第几行开始（1-indexed，默认 1）
    """

    @property
    def name(self) -> str:
        return "file_view"

    @property
    def description(self) -> str:
        return (
            f"View a specific section of a file, {VIEW_WINDOW_LINES} lines at a time. "
            f"Use start_line to scroll through large files. Lines are 1-indexed."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file",
                },
                "start_line": {
                    "type": "integer",
                    "description": f"First line to show (1-indexed, default 1)",
                },
            },
            "required": ["path"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        path = Path(params.get("path", ""))
        start_line = max(1, int(params.get("start_line", 1)))

        if not path.exists():
            return ToolResult(success=False, output="", error=f"File not found: {path}")
        if not path.is_file():
            return ToolResult(success=False, output="", error=f"Not a file: {path}")

        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as e:
            return ToolResult(success=False, output="", error=str(e), failure_type=FAILURE_TYPE_WORKSPACE_ERROR)

        total = len(lines)
        if start_line > total:
            return ToolResult(
                success=False,
                output="",
                error=f"start_line {start_line} exceeds file length ({total} lines)",
            )

        end_line = min(start_line + VIEW_WINDOW_LINES - 1, total)
        window = lines[start_line - 1 : end_line]

        numbered = "\n".join(
            f"{start_line + i:4d} | {line}"
            for i, line in enumerate(window)
        )

        nav = ""
        if end_line < total:
            nav = f"\n[Lines {start_line}–{end_line} of {total}. Next: file_view path={path} start_line={end_line + 1}]"
        else:
            nav = f"\n[Lines {start_line}–{end_line} of {total}. End of file.]"

        return ToolResult(success=True, output=numbered + nav)


class FileWriteTool(BaseTool):
    """
    写入文件（全量覆盖）。自动创建父目录。

    params:
        path (str):    文件路径
        content (str): 要写入的内容
    """

    @property
    def name(self) -> str:
        return "file_write"

    @property
    def description(self) -> str:
        return (
            "Write content to a file, replacing its entire contents. "
            "Use this only when you intentionally want a full-file rewrite; "
            "prefer apply_patch for targeted edits. "
            "Parent directories are created automatically. "
            "Always read the file first before writing to avoid losing existing content."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to write",
                },
                "content": {
                    "type": "string",
                    "description": "Full content to write to the file",
                },
            },
            "required": ["path", "content"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        path = Path(params.get("path", ""))
        content = params.get("content", "")

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError as e:
            return ToolResult(success=False, output="", error=str(e), failure_type=FAILURE_TYPE_WORKSPACE_ERROR)

        line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        return ToolResult(
            success=True,
            output=f"Written {line_count} lines to {path}",
        )


class ApplyPatchTool(BaseTool):
    """
    执行结构化 Patch 对象。

    支持：
    - replace_file
    - search_replace
    - replace_range
    """

    @property
    def name(self) -> str:
        return "apply_patch"

    @property
    def description(self) -> str:
        return (
            "Apply a structured patch to a file. Prefer this tool for most code edits. "
            "Use patch_type=replace_file for full rewrites, "
            "search_replace for targeted text substitution, "
            "or replace_range for line-based edits."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "patch_type": {
                    "type": "string",
                    "description": "Patch operation: replace_file | search_replace | replace_range",
                },
                "path": {
                    "type": "string",
                    "description": "Target file path",
                },
                "content": {
                    "type": "string",
                    "description": "Full replacement content for replace_file",
                },
                "search": {
                    "type": "string",
                    "description": "Text to search for in search_replace mode",
                },
                "replace": {
                    "type": "string",
                    "description": "Replacement text for search_replace or replace_range",
                },
                "start_line": {
                    "type": "integer",
                    "description": "1-indexed start line for replace_range",
                },
                "end_line": {
                    "type": "integer",
                    "description": "1-indexed end line for replace_range",
                },
                "all_occurrences": {
                    "type": "boolean",
                    "description": "Replace all search matches instead of only the first one",
                },
                "expected_content": {
                    "type": "string",
                    "description": "Optional full-file precondition for conflict detection before applying the patch",
                },
                "expected_lines": {
                    "type": "string",
                    "description": "Optional exact text expected in the target line range before replace_range is applied",
                },
                "expected_occurrences": {
                    "type": "integer",
                    "description": "Optional expected number of search matches before search_replace is applied",
                },
            },
            "required": ["patch_type", "path"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        try:
            patch = Patch.from_params(params)
        except ValueError as e:
            return ToolResult(success=False, output="", error=str(e), failure_type=FAILURE_TYPE_TOOL_FAILURE)

        path = patch.target_path()
        before = ""
        existed = path.exists()
        if existed:
            if not path.is_file():
                return ToolResult(success=False, output="", error=f"Not a file: {path}", failure_type=FAILURE_TYPE_TOOL_FAILURE)
            try:
                before = path.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                return ToolResult(success=False, output="", error=str(e), failure_type=FAILURE_TYPE_WORKSPACE_ERROR)

        try:
            after, stats = self._apply(patch, before, existed)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(after, encoding="utf-8")
        except ValueError as e:
            failure_type = FAILURE_TYPE_PATCH_CONFLICT if "conflict" in str(e).lower() else FAILURE_TYPE_TOOL_FAILURE
            return ToolResult(
                success=False,
                output="",
                error=str(e),
                metadata={"patch": patch.to_dict()},
                failure_type=failure_type,
            )
        except OSError as e:
            return ToolResult(
                success=False,
                output="",
                error=str(e),
                metadata={"patch": patch.to_dict()},
                failure_type=FAILURE_TYPE_WORKSPACE_ERROR,
            )

        line_count = after.count("\n") + (1 if after and not after.endswith("\n") else 0)
        metadata = {
            "patch": patch.to_dict(),
            "reverse_patch": self._build_reverse_patch(path, before),
            "path": str(path),
            "line_count": line_count,
            "previous_exists": existed,
            "stats": stats,
        }
        return ToolResult(
            success=True,
            output=f"Applied {patch.patch_type} to {path} ({line_count} lines)",
            metadata=metadata,
        )

    def _apply(self, patch: Patch, before: str, existed: bool) -> tuple[str, dict[str, Any]]:
        if patch.patch_type == "replace_file":
            if patch.content is None:
                raise ValueError("content is required for replace_file")
            if patch.expected_content is not None and before != patch.expected_content:
                raise ValueError(f"Patch conflict for {patch.path}: file contents changed")
            return patch.content, {"operation": "replace_file"}

        if not existed:
            raise ValueError(f"File not found: {patch.path}")

        if patch.patch_type == "search_replace":
            if patch.search is None or patch.replace is None:
                raise ValueError("search and replace are required for search_replace")
            occurrences = before.count(patch.search)
            if (
                patch.expected_occurrences is not None
                and occurrences != patch.expected_occurrences
            ):
                raise ValueError(
                    f"Patch conflict for {patch.path}: expected {patch.expected_occurrences} matches, found {occurrences}"
                )
            if occurrences == 0:
                raise ValueError(f"Search text not found in {patch.path}")
            if patch.all_occurrences:
                after = before.replace(patch.search, patch.replace)
                replaced = occurrences
            else:
                after = before.replace(patch.search, patch.replace, 1)
                replaced = 1
            return after, {
                "operation": "search_replace",
                "occurrences_found": occurrences,
                "occurrences_replaced": replaced,
            }

        if patch.patch_type == "replace_range":
            if patch.start_line is None or patch.end_line is None:
                raise ValueError("start_line and end_line are required for replace_range")
            if patch.start_line < 1 or patch.end_line < patch.start_line:
                raise ValueError("Invalid line range for replace_range")
            replacement = patch.replace or ""
            lines = before.splitlines(keepends=True)
            total = len(lines)
            if patch.end_line > total:
                raise ValueError(
                    f"replace_range end_line {patch.end_line} exceeds file length ({total} lines)"
                )
            if patch.expected_lines is not None:
                current_slice = "".join(lines[patch.start_line - 1 : patch.end_line])
                if current_slice != patch.expected_lines:
                    raise ValueError(
                        f"Patch conflict for {patch.path}: target lines no longer match expected content"
                    )
            replacement_lines = replacement.splitlines(keepends=True)
            if replacement and not replacement.endswith(("\n", "\r")):
                replacement_lines = replacement_lines[:-1] + [replacement_lines[-1]]
            after_lines = (
                lines[: patch.start_line - 1]
                + replacement_lines
                + lines[patch.end_line :]
            )
            return "".join(after_lines), {
                "operation": "replace_range",
                "start_line": patch.start_line,
                "end_line": patch.end_line,
            }

        raise ValueError(f"Unsupported patch_type: {patch.patch_type}")

    def _build_reverse_patch(self, path: Path, before: str) -> dict[str, Any]:
        return {
            "patch_type": "replace_file",
            "path": str(path),
            "content": before,
        }


class RevertPatchTool(BaseTool):
    """Revert a previously applied patch using its reverse_patch metadata."""

    @property
    def name(self) -> str:
        return "revert_patch"

    @property
    def description(self) -> str:
        return (
            "Revert a previously applied patch using the reverse_patch object "
            "returned by apply_patch."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "reverse_patch": {
                    "type": "object",
                    "description": "Reverse patch metadata returned by apply_patch",
                },
            },
            "required": ["reverse_patch"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        reverse_patch = params.get("reverse_patch")
        if not isinstance(reverse_patch, dict):
            return ToolResult(success=False, output="", error="reverse_patch is required", failure_type=FAILURE_TYPE_TOOL_FAILURE)
        return ApplyPatchTool().execute(reverse_patch)
