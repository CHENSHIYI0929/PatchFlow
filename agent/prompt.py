"""
agent/prompt.py

System prompt 模板管理。

职责：
- 维护 agent 的 system prompt 模板
- 根据运行时信息（工具列表、repo 概况）渲染最终 prompt
- 提供 Reflection prompt 模板

设计原则：
- prompt 集中在这里，修改 prompt 不需要改 core.py
- 模板用 str.format() 而不是 jinja2，减少依赖
- 每个 prompt 都有对应的函数，便于测试和调整
"""

from __future__ import annotations

from llm.base import LLMToolSchema


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_TEMPLATE = """\
You are an autonomous coding agent. Your goal is to understand a coding task, \
explore the repository, make the necessary code changes, and verify they work correctly.

## Workflow
1. **Explore**: Understand the repository structure and the problem
2. **Plan**: Identify what needs to change and why
3. **Edit**: Make precise, minimal changes using the available tools
4. **Verify**: Run tests to confirm the fix works
5. **Finish**: Call finish with a clear summary of what you changed

## Rules
- Think step by step before each action (use the thought field)
- Prefer structured edits with apply_patch for code changes; use file_write only when you intend to replace an entire file
- When tests fail or you need to expand beyond one file, use graph_neighbors to inspect imported/importing files and symbol relationships
- When you apply a patch, pay attention to conflict checks in the result metadata; if a patch was wrong, use revert_patch with the provided reverse_patch
- Before apply_patch or file_write, include an EDIT_PLAN JSON object in your thought with target_files, change_intent, expected_behavior, risk_level, and tests_to_run
- After editing files, always run tests to verify your changes
- If tests fail, read the error carefully and fix the root cause, not the symptom
- If you are stuck after several attempts, reflect on your approach and try differently
- Make the smallest change that fixes the problem
- When done, call finish. If you truly cannot solve it, call give_up with an explanation

## Repository
Path: {repo_path}
{repo_summary}

## Available tools
{tool_descriptions}
"""

_NO_REPO_SUMMARY = "(Repository summary not yet available — use find_files and file_read to explore)"


def build_system_prompt(
    repo_path: str,
    tools: list[LLMToolSchema],
    repo_summary: str | None = None,
) -> str:
    """
    渲染完整的 system prompt。

    Args:
        repo_path:    repo 根目录路径
        tools:        已注册工具的 schema 列表
        repo_summary: repo-map 生成的摘要（Day 5 接入，当前传 None）

    Returns:
        渲染好的 system prompt 字符串
    """
    tool_descriptions = _format_tool_descriptions(tools)
    summary = repo_summary or _NO_REPO_SUMMARY

    return _SYSTEM_TEMPLATE.format(
        repo_path=repo_path,
        repo_summary=summary,
        tool_descriptions=tool_descriptions,
    )


def _format_tool_descriptions(tools: list[LLMToolSchema]) -> str:
    """把工具列表格式化为易读的描述块。"""
    if not tools:
        return "(no tools available)"
    lines = []
    for tool in tools:
        lines.append(f"- **{tool.name}**: {tool.description}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Reflection prompts
# ---------------------------------------------------------------------------

REFLECTION_TEST_FAILED = """\
[REFLECTION] The tests just failed. Before your next action, consider:
1. Read the full error message above carefully — what is the root cause?
2. Is your last edit correct? Did it introduce a new bug?
3. Do you need to look at more context before editing again?
4. If the failure may involve another module, inspect graph neighbors of the failing file or symbol before editing again.

Be specific about what you will do differently. What is your next action?\
"""

REFLECTION_NO_EDIT = """\
[REFLECTION] You have taken {n} steps without editing any file.
You may be stuck in an exploration loop. Consider:
1. Do you have enough context to make a change now?
2. If yes — make the edit
3. If no — identify exactly what you still need, get it in one targeted step, then edit

What specific action will move the task forward?\
"""

REFLECTION_LOOP_DETECTED = """\
[REFLECTION] You have repeated the same action {n} times in a row.
This suggests you are stuck. Stop and reconsider:
1. What are you trying to achieve with this action?
2. Why isn't it working?
3. What completely different approach could you try?

Do not repeat the same action again.\
"""

REFLECTION_VERIFICATION_FAILED = """\
[REFLECTION] The finish verification failed, so the task is not complete yet.
Use the failure output above as the next debugging target. Re-read the failing test or traceback,
inspect the relevant source, make a focused fix, and run the targeted verification again before finishing.\
"""

_RECOVERY_PROMPTS = {
    "patch_conflict": (
        "[RECOVERY] The patch conflicted with the current file contents. "
        "Read the target file again, recompute the smallest patch against the current content, then retry."
    ),
    "timeout": (
        "[RECOVERY] The command timed out. Narrow the command to the most relevant test or file, "
        "or inspect the code path before trying a longer run."
    ),
    "tool_failure": (
        "[RECOVERY] The last tool call failed. Check the tool name and parameters, then try a corrected, targeted action."
    ),
    "workspace_error": (
        "[RECOVERY] The workspace operation failed. Verify paths and repository state before editing again."
    ),
    "verification_failed": (
        "[RECOVERY] Verification failed. Use the failing assertion or traceback to locate the root cause before editing."
    ),
}


def reflection_test_failed() -> str:
    return REFLECTION_TEST_FAILED


def reflection_no_edit(n: int) -> str:
    return REFLECTION_NO_EDIT.format(n=n)


def reflection_loop_detected(n: int) -> str:
    return REFLECTION_LOOP_DETECTED.format(n=n)


def reflection_verification_failed() -> str:
    return REFLECTION_VERIFICATION_FAILED


def recovery_prompt_for_failure(failure_type: str | None) -> str | None:
    if not failure_type:
        return None
    return _RECOVERY_PROMPTS.get(failure_type)


# ---------------------------------------------------------------------------
# Task prompt（用户消息，描述任务）
# ---------------------------------------------------------------------------

_TASK_TEMPLATE = """\
Please fix the following issue in the repository at {repo_path}.

## Task
{description}
{issue_section}
{verification_section}
{exclude_section}
{targets_section}
{benchmark_verification_section}
## Instructions
- Start by exploring the repository to understand the codebase
- Prefer verifying the task with the targeted test command first when one is provided
- Prefer apply_patch for targeted edits; use file_write only for full-file rewrites
- If a targeted test fails, use graph_neighbors or symbol search to inspect adjacent modules before making broad edits
- Before any write action, include EDIT_PLAN: {{"target_files":["..."],"change_intent":"...","expected_behavior":"...","risk_level":"low|medium|high","tests_to_run":["..."]}}
- Make the minimal changes necessary to fix the issue
- Run the tests to verify your fix works
- If the targeted verification already passes before any edit and the code already satisfies the task, finish instead of continuing to explore
- When complete, call finish with a summary of your changes\
"""

_ISSUE_SECTION_TEMPLATE = """
## GitHub Issue
URL: {issue_url}
"""

_VERIFICATION_SECTION_TEMPLATE = """
## Target Verification
Prefer this exact verification command or scope first:
{test_cmd}
"""

_EXCLUDE_SECTION_TEMPLATE = """
## Excluded Paths
Avoid spending repo-map or exploration budget on these unrelated paths unless absolutely necessary:
{exclude_paths}
"""

_TARGET_FILES_SECTION_TEMPLATE = """
## Target Files
These files are especially likely to matter for this task:
{target_files}
"""

_BENCHMARK_VERIFICATION_SECTION_TEMPLATE = """
## Benchmark Verification Rules
- In benchmark mode, use the `verify_task` tool for verification once you have a candidate fix
- Do not run broader pytest commands or full test files on your own
- The configured task verification is the success criterion for this run
"""


def build_task_prompt(
    description: str,
    repo_path: str,
    issue_url: str | None = None,
    test_cmd: str | None = None,
    exclude_paths: list[str] | None = None,
    target_files: list[str] | None = None,
    run_mode: str = "auto",
) -> str:
    """
    构建任务描述的用户消息（对话的第一条 user 消息）。

    Args:
        description: 任务描述（自然语言）
        repo_path:   repo 根目录
        issue_url:   GitHub issue URL（可选）
    """
    issue_section = ""
    if issue_url:
        issue_section = _ISSUE_SECTION_TEMPLATE.format(issue_url=issue_url)
    verification_section = ""
    if test_cmd:
        verification_section = _VERIFICATION_SECTION_TEMPLATE.format(test_cmd=test_cmd)
    exclude_section = ""
    if exclude_paths:
        exclude_section = _EXCLUDE_SECTION_TEMPLATE.format(
            exclude_paths="\n".join(f"- {path}" for path in exclude_paths)
        )
    targets_section = ""
    if target_files:
        targets_section = _TARGET_FILES_SECTION_TEMPLATE.format(
            target_files="\n".join(f"- {path}" for path in target_files)
        )
    benchmark_verification_section = ""
    if run_mode == "benchmark" and test_cmd:
        benchmark_verification_section = _BENCHMARK_VERIFICATION_SECTION_TEMPLATE

    return _TASK_TEMPLATE.format(
        repo_path=repo_path,
        description=description.strip(),
        issue_section=issue_section,
        verification_section=verification_section,
        exclude_section=exclude_section,
        targets_section=targets_section,
        benchmark_verification_section=benchmark_verification_section,
    )
