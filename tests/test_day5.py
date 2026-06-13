"""
tests/test_day5.py

Day 5 测试：RepoMap、TokenBudget、ConversationHistory，以及 core.py 集成。
"""

from __future__ import annotations

from pathlib import Path
import json

import pytest

from agent.memory import append_run_memory, format_memory_hits, load_experience_memories, search_memories
from context.history import ConversationHistory
from context.repo_map import RepoMap, _extract_python_symbols, _extract_symbols_regex
from context.token_budget import TokenBudget, estimate_tokens
from llm.base import LLMMessage, MockBackend
from agent.task import Action, ActionType, RunResult, RunStatus, Task, ToolCall
from tools.base import NoopTool, ToolRegistry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def py_repo(tmp_path) -> Path:
    """包含几个 Python 文件的示例 repo。"""
    (tmp_path / "main.py").write_text(
        "def run():\n    pass\n\nclass App:\n    def start(self):\n        pass\n"
    )
    (tmp_path / "utils.py").write_text(
        "def helper():\n    return 1\n\ndef another():\n    pass\n"
    )
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "module.py").write_text("class SubModule:\n    pass\n")
    # 应被跳过
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "main.cpython-312.pyc").write_bytes(b"\x00" * 10)
    return tmp_path


# ===========================================================================
# estimate_tokens
# ===========================================================================

class TestEstimateTokens:
    def test_empty_string(self):
        assert estimate_tokens("") >= 1

    def test_short_string(self):
        assert estimate_tokens("hello") >= 1

    def test_longer_string(self):
        text = "a" * 400
        # 不断言具体值，只断言比短文本多
        short = estimate_tokens("a" * 10)
        assert estimate_tokens(text) > short

    def test_proportional(self):
        # tiktoken 对重复字符有压缩，不严格线性
        # 只断言更长的文本 token 数更多
        t1 = estimate_tokens("hello world " * 10)
        t2 = estimate_tokens("hello world " * 20)
        assert t2 > t1


# ===========================================================================
# RepoMap
# ===========================================================================

class TestRepoMap:
    def test_build_returns_string(self, py_repo):
        rm = RepoMap(py_repo)
        result = rm.build(budget=10_000)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_contains_file_names(self, py_repo):
        rm = RepoMap(py_repo)
        result = rm.build(budget=10_000)
        assert "main.py" in result
        assert "utils.py" in result

    def test_contains_symbol_names(self, py_repo):
        rm = RepoMap(py_repo)
        result = rm.build(budget=10_000)
        assert "run" in result
        assert "App" in result
        assert "helper" in result

    def test_skips_pycache(self, py_repo):
        rm = RepoMap(py_repo)
        result = rm.build(budget=10_000)
        assert "__pycache__" not in result

    def test_skips_logs_directory(self, tmp_path):
        (tmp_path / "app.py").write_text("def run():\n    pass\n", encoding="utf-8")
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "events.json").write_text('{"ok": true}', encoding="utf-8")

        rm = RepoMap(tmp_path)
        result = rm.build(budget=10_000)

        assert "app.py" in result
        assert "events.json" not in result

    def test_respects_exclude_paths(self, tmp_path):
        (tmp_path / "keep.py").write_text("def keep():\n    pass\n", encoding="utf-8")
        (tmp_path / "README.md").write_text("# docs\n", encoding="utf-8")
        ignored = tmp_path / "fixtures"
        ignored.mkdir()
        (ignored / "noise.py").write_text("def noise():\n    pass\n", encoding="utf-8")

        rm = RepoMap(tmp_path, exclude_paths=["fixtures", "README.md"])
        result = rm.build(budget=10_000)
        trace = rm.last_trace()

        assert "keep.py" in result
        assert "noise.py" not in result
        assert "README.md" not in result
        assert all(chunk["path"] != "fixtures/noise.py" for chunk in trace["chunks"])

    def test_budget_limits_output(self, py_repo):
        rm = RepoMap(py_repo)
        # 非常小的预算，输出应该被截断
        result = rm.build(budget=10)
        assert len(result) <= 10 * 4 + 100  # 留一点 truncation message 的余量

    def test_empty_repo(self, tmp_path):
        rm = RepoMap(tmp_path)
        result = rm.build()
        assert "empty" in result.lower()

    def test_nonexistent_path_graceful(self, tmp_path):
        # 不存在的 path 不应崩溃
        rm = RepoMap(tmp_path / "no_such_dir")
        result = rm.build()
        assert isinstance(result, str)

    def test_large_file_skipped(self, tmp_path):
        # 超过 500KB 的文件应被跳过
        big = tmp_path / "big.py"
        big.write_bytes(b"x = 1\n" * 100_000)   # ~600KB
        rm = RepoMap(tmp_path)
        result = rm.build()
        assert "big.py" not in result

    def test_last_trace_contains_chunks(self, py_repo):
        rm = RepoMap(py_repo)
        rm.build(budget=10_000)
        trace = rm.last_trace()
        assert trace["chunk_count"] >= 1
        assert any(chunk["path"] == "main.py" for chunk in trace["chunks"])
        main_chunk = next(chunk for chunk in trace["chunks"] if chunk["path"] == "main.py")
        assert main_chunk["chunk_id"] == "repo-map:main.py"
        assert any(sym["name"] == "run" for sym in main_chunk["symbols"])

    def test_python_graph_edges_in_trace(self, tmp_path):
        (tmp_path / "helper.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
        (tmp_path / "main.py").write_text(
            "from helper import helper\n\n\ndef run():\n    return helper()\n",
            encoding="utf-8",
        )

        rm = RepoMap(tmp_path)
        rm.build(budget=10_000)
        trace = rm.last_trace()
        main_chunk = next(chunk for chunk in trace["chunks"] if chunk["path"] == "main.py")
        helper_chunk = next(chunk for chunk in trace["chunks"] if chunk["path"] == "helper.py")

        assert "helper.py" in main_chunk["imports"]
        assert "main.py" in helper_chunk["imported_by"]
        assert any(ref["name"] == "helper" for ref in main_chunk["referenced_symbols"])
        assert "main.py" in helper_chunk["referenced_by"]


class TestExtractPythonSymbols:
    def test_extracts_function(self, tmp_path):
        code = "def foo():\n    pass\n"
        syms = _extract_python_symbols(code, Path("test.py"))
        names = [s.name for s in syms]
        assert "foo" in names

    def test_extracts_class(self, tmp_path):
        code = "class MyClass:\n    pass\n"
        syms = _extract_python_symbols(code, Path("test.py"))
        names = [s.name for s in syms]
        assert "MyClass" in names

    def test_method_has_indent(self, tmp_path):
        code = "class Foo:\n    def bar(self):\n        pass\n"
        syms = _extract_python_symbols(code, Path("test.py"))
        bar = next((s for s in syms if s.name == "bar"), None)
        assert bar is not None
        assert bar.indent > 0
        assert not bar.is_toplevel

    def test_toplevel_function_no_indent(self, tmp_path):
        code = "def baz():\n    pass\n"
        syms = _extract_python_symbols(code, Path("test.py"))
        baz = next((s for s in syms if s.name == "baz"), None)
        assert baz is not None
        assert baz.is_toplevel

    def test_line_numbers_correct(self, tmp_path):
        code = "# comment\ndef foo():\n    pass\n"
        syms = _extract_python_symbols(code, Path("test.py"))
        foo = next((s for s in syms if s.name == "foo"), None)
        assert foo is not None
        assert foo.line == 2

    def test_syntax_error_falls_back_to_regex(self, tmp_path):
        # 语法错误的代码应 fallback 到正则，不崩溃
        code = "def foo(\n    # broken syntax"
        syms = _extract_python_symbols(code, Path("test.py"))
        assert isinstance(syms, list)


class TestExtractSymbolsRegex:
    def test_extracts_def(self):
        code = "def my_func():\n    pass\n"
        syms = _extract_symbols_regex(code, Path("test.py"))
        assert any(s.name == "my_func" for s in syms)

    def test_extracts_class(self):
        code = "class MyClass:\n    pass\n"
        syms = _extract_symbols_regex(code, Path("test.py"))
        assert any(s.name == "MyClass" for s in syms)

    def test_javascript_function(self):
        code = "function myFunc() {\n    return 1;\n}\n"
        syms = _extract_symbols_regex(code, Path("test.js"))
        assert any(s.name == "myFunc" for s in syms)


# ===========================================================================
# TokenBudget
# ===========================================================================

class TestTokenBudget:
    def test_default_plan_sums_to_budget(self):
        budget = TokenBudget(total=80_000)
        plan = budget.default_plan()
        assert plan.total == 80_000
        assert plan.reserve > 0
        assert plan.system_core + plan.repo_map + plan.history + plan.observation <= plan.available

    def test_trim_to_short_text_unchanged(self):
        budget = TokenBudget()
        text = "hello world"
        assert budget.trim_to(text, token_limit=1000) == text

    def test_trim_to_long_text_truncated(self):
        budget = TokenBudget()
        text = "x" * 10_000
        result = budget.trim_to(text, token_limit=100)
        assert len(result) < len(text)
        assert "truncated" in result

    def test_trim_history_short_unchanged(self):
        budget = TokenBudget()
        msgs = [
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "ok"},
        ]
        result = budget.trim_history(msgs, token_limit=10_000)
        assert len(result) == 2

    def test_trim_history_preserves_first_message(self):
        budget = TokenBudget(total=1000)
        # 第一条很短，后面很多长消息
        msgs = [{"role": "user", "content": "task"}]
        for i in range(20):
            msgs.append({"role": "user", "content": "x" * 200})
        result = budget.trim_history(msgs, token_limit=50)
        assert result[0]["content"] == "task"

    def test_trim_history_keeps_recent_messages(self):
        budget = TokenBudget()
        msgs = [{"role": "user", "content": "task"}]
        for i in range(10):
            msgs.append({"role": "user", "content": f"message {i}"})
        # 预算只够放 3 条
        result = budget.trim_history(msgs, token_limit=15)
        contents = [m["content"] for m in result]
        # 最新的消息（message 9）应该在
        assert any("message 9" in c for c in contents)

    def test_trim_history_adds_truncation_notice(self):
        budget = TokenBudget()
        msgs = [{"role": "user", "content": "task"}]
        for i in range(20):
            msgs.append({"role": "user", "content": "x" * 100})
        result = budget.trim_history(msgs, token_limit=50)
        # 应该有一条截断提示消息
        contents = " ".join(m["content"] for m in result)
        assert "truncated" in contents.lower()

    def test_trim_history_empty(self):
        budget = TokenBudget()
        assert budget.trim_history([], token_limit=1000) == []

    def test_usage_report(self):
        budget = TokenBudget(total=10_000)
        report = budget.usage_report("system", "repo", [], "obs")
        assert "system" in report
        assert "total" in report
        assert report["budget"] == 10_000


# ===========================================================================
# ConversationHistory
# ===========================================================================

class TestConversationHistory:
    def test_add_and_retrieve(self):
        h = ConversationHistory(max_messages=10)
        h.add(LLMMessage(role="user", content="hello"))
        assert h.message_count == 1
        assert h.to_list()[0].content == "hello"

    def test_sliding_window_drops_oldest(self):
        h = ConversationHistory(max_messages=3)
        h.add(LLMMessage(role="user", content="first"))
        h.add(LLMMessage(role="user", content="second"))
        h.add(LLMMessage(role="user", content="third"))
        h.add(LLMMessage(role="user", content="fourth"))
        # 最多 3 条，first 应该被丢弃（但保留 index 0）
        assert h.message_count == 3
        contents = [m.content for m in h.to_list()]
        assert "first" in contents      # index 0 永不丢弃
        assert "second" not in contents  # 被丢弃
        assert "fourth" in contents

    def test_sliding_window_compresses_dropped_context(self):
        h = ConversationHistory(max_messages=3)
        h.add(LLMMessage(role="user", content="Task: fix parser.py"))
        h.add(LLMMessage(role="assistant", content="Thought: inspect parser\nAction: file_read\nParams: {}"))
        h.add(LLMMessage(role="user", content="Observation: parser.py has parse_date"))
        h.add(LLMMessage(role="user", content="Traceback: parser.py failed with ValueError"))

        assert h.compressed_message_count == 1
        summary = h.compressed_summary
        assert "COMPRESSED CONTEXT" in summary
        assert "file_read" in summary
        dicts = h.to_dicts()
        assert dicts[1]["content"].startswith("[COMPRESSED CONTEXT]")

    def test_compression_preserves_long_memory_patterns_and_failure_trajectory(self):
        h = ConversationHistory(max_messages=3)
        h.add(LLMMessage(role="user", content="Task: fix parser.py"))
        h.add(LLMMessage(
            role="user",
            content=(
                "[LONG MEMORY] Relevant prior run memories:\n"
                "Successful patterns for category=bugfix:\n"
                "- Prefer targeted verification first: pytest tests/test_parser.py -q\n"
                "Recovery hints for failure_type=verification_failed:\n"
                "- Previous failure was classified as verification_failed; recover using the matching taxonomy strategy."
            ),
        ))
        h.add(LLMMessage(role="user", content="[Tool: test | ERROR]\nFAILED tests/test_parser.py::test_empty - AssertionError"))
        h.add(LLMMessage(role="user", content="[REFLECTION] Re-check parser.py before retrying the patch."))
        h.add(LLMMessage(role="assistant", content="Thought: inspect failure\nAction: file_read"))

        summary = h.compressed_summary
        assert "Prior successful patterns" in summary
        assert "Prefer targeted verification first" in summary
        assert "Failure trajectory to preserve" in summary
        assert "verification_failed" in summary
        assert "FAILED tests/test_parser.py::test_empty" in summary

    def test_first_message_never_dropped(self):
        h = ConversationHistory(max_messages=2)
        h.add(LLMMessage(role="user", content="task_description"))
        for i in range(10):
            h.add(LLMMessage(role="user", content=f"msg_{i}"))
        assert h.to_list()[0].content == "task_description"

    def test_to_dicts(self):
        h = ConversationHistory()
        h.add(LLMMessage(role="user", content="hello"))
        dicts = h.to_dicts()
        assert dicts == [{"role": "user", "content": "hello"}]

    def test_compression_can_be_disabled(self):
        h = ConversationHistory(max_messages=2, enable_compression=False)
        h.add(LLMMessage(role="user", content="task"))
        h.add(LLMMessage(role="assistant", content="Thought: inspect\nAction: file_read"))
        h.add(LLMMessage(role="user", content="Observation: old.py"))

        assert h.compressed_message_count == 0
        assert len(h.to_list()) == 2


class TestLongMemory:
    def test_search_memories_finds_related_task(self, tmp_path):
        task = Task(
            task_id="mem001",
            description="Fix parser empty string handling",
            repo_path=str(tmp_path),
            task_category="bugfix",
            expected_failure_type="verification_failed",
            test_cmd="pytest tests/test_parser.py -q",
        )
        result = RunResult(
            task_id=task.task_id,
            status=RunStatus.SUCCESS,
            summary="Fixed parser empty string handling",
            steps_taken=3,
            total_tokens=100,
        )
        append_run_memory(tmp_path / "logs", task, result)

        hits = search_memories(
            tmp_path / "logs",
            query="parser empty string bug",
            repo_path=tmp_path,
            category="bugfix",
            failure_type="verification_failed",
            limit=3,
        )

        assert hits
        text = format_memory_hits(hits, category="bugfix", failure_type="verification_failed")
        assert "LONG MEMORY" in text
        assert "pytest tests/test_parser.py -q" in text
        assert "Successful patterns for category=bugfix" in text
        assert "- Prefer targeted verification first: pytest tests/test_parser.py -q" in text

    def test_search_memories_prioritizes_matching_category_and_failure_type(self, tmp_path):
        related = Task(
            task_id="mem-cat-a",
            description="Fix parser empty string handling",
            repo_path=str(tmp_path),
            task_category="bugfix",
            expected_failure_type="verification_failed",
            test_cmd="pytest tests/test_parser.py -q",
        )
        unrelated = Task(
            task_id="mem-cat-b",
            description="Fix parser empty string handling",
            repo_path=str(tmp_path),
            task_category="refactor_safe",
            expected_failure_type="verification_failed",
            test_cmd="pytest tests/test_parser.py -q",
        )
        related_success = RunResult(
            task_id=related.task_id,
            status=RunStatus.SUCCESS,
            summary="Fixed parser empty string handling",
            steps_taken=3,
            total_tokens=100,
        )
        unrelated_success = RunResult(
            task_id=unrelated.task_id,
            status=RunStatus.SUCCESS,
            summary="Fixed parser empty string handling",
            steps_taken=3,
            total_tokens=100,
        )
        append_run_memory(tmp_path / "logs", unrelated, unrelated_success)
        append_run_memory(tmp_path / "logs", related, related_success)

        hits = search_memories(
            tmp_path / "logs",
            query="parser empty string bug",
            repo_path=tmp_path,
            category="bugfix",
            failure_type="verification_failed",
            limit=2,
        )

        assert hits
        assert hits[0].payload["task_category"] == "bugfix"

    def test_memory_file_is_jsonl(self, tmp_path):
        task = Task(
            task_id="mem002",
            description="Fix auth",
            repo_path=str(tmp_path),
            task_category="bugfix",
            expected_failure_type="verification_failed",
        )
        result = RunResult(
            task_id=task.task_id,
            status=RunStatus.FAILED,
            summary="failed",
            steps_taken=1,
            total_tokens=10,
            failure_type="verification_failed",
        )

        path = append_run_memory(tmp_path / "logs", task, result)

        row = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
        assert row["task_description"] == "Fix auth"
        assert row["task_category"] == "bugfix"
        assert row["expected_failure_type"] == "verification_failed"
        assert row["lessons"]

    def test_successful_runs_append_experience_memory(self, tmp_path):
        task = Task(
            task_id="memexp1",
            description="Fix parser",
            repo_path=str(tmp_path),
            task_category="bugfix",
            expected_failure_type="verification_failed",
            test_cmd="pytest tests/test_parser.py -q",
        )
        result = RunResult(
            task_id=task.task_id,
            status=RunStatus.SUCCESS,
            summary="Fixed parser",
            steps_taken=2,
            total_tokens=10,
        )

        append_run_memory(tmp_path / "logs", task, result)

        rows = load_experience_memories(tmp_path / "logs", limit=5)
        assert rows
        assert rows[-1]["memory_kind"] == "experience"
        assert rows[-1]["task_category"] == "bugfix"
        assert rows[-1]["test_cmd"] == "pytest tests/test_parser.py -q"

    def test_experience_memory_deduplicates_same_success_pattern(self, tmp_path):
        task = Task(
            task_id="memexp2",
            description="Fix parser",
            repo_path=str(tmp_path),
            task_category="bugfix",
            expected_failure_type="verification_failed",
            test_cmd="pytest tests/test_parser.py -q",
        )
        result = RunResult(
            task_id=task.task_id,
            status=RunStatus.SUCCESS,
            summary="Fixed parser",
            steps_taken=2,
            total_tokens=10,
        )

        append_run_memory(tmp_path / "logs", task, result)
        append_run_memory(tmp_path / "logs", task, result)

        rows = load_experience_memories(tmp_path / "logs", limit=10)
        assert len(rows) == 1

    def test_from_dicts(self):
        dicts = [{"role": "user", "content": "task"}, {"role": "assistant", "content": "ok"}]
        h = ConversationHistory.from_dicts(dicts)
        assert h.message_count == 2
        assert h.to_list()[0].content == "task"

    def test_add_many(self):
        h = ConversationHistory(max_messages=5)
        msgs = [LLMMessage(role="user", content=f"m{i}") for i in range(3)]
        h.add_many(msgs)
        assert h.message_count == 3

    def test_clear_except_first(self):
        h = ConversationHistory()
        h.add(LLMMessage(role="user", content="task"))
        h.add(LLMMessage(role="assistant", content="ok"))
        h.add(LLMMessage(role="user", content="more"))
        h.clear_except_first()
        assert h.message_count == 1
        assert h.to_list()[0].content == "task"

    def test_last_message(self):
        h = ConversationHistory()
        h.add(LLMMessage(role="user", content="first"))
        h.add(LLMMessage(role="assistant", content="last"))
        assert h.last_message.content == "last"

    def test_empty_history_last_message_is_none(self):
        h = ConversationHistory()
        assert h.last_message is None

    def test_len(self):
        h = ConversationHistory()
        h.add(LLMMessage(role="user", content="x"))
        assert len(h) == 1


# ===========================================================================
# core.py 集成：context 模块接入后仍能正常运行
# ===========================================================================

class TestCoreWithContext:
    def _make_task(self, tmp_path) -> Task:
        return Task(
            task_id="ctx001",
            description="Fix the bug",
            repo_path=str(tmp_path),
            max_steps=5,
            budget_tokens=80_000,
        )

    def test_run_with_context_succeeds(self, tmp_path):
        from agent.core import Agent, AgentConfig
        from agent.event_log import EventLog

        task = self._make_task(tmp_path)
        registry = ToolRegistry().register(NoopTool("shell"))
        script = [
            Action(ActionType.TOOL_CALL, "explore", ToolCall("shell", {"cmd": "ls"})),
            Action(ActionType.FINISH, "done", message="Task complete"),
        ]
        backend = MockBackend(script)
        config = AgentConfig(budget_tokens=80_000, history_max_messages=20)
        agent = Agent(backend, registry, config)

        with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
            result = agent.run(task, log)

        assert result.is_success()

    def test_repo_map_injected_in_system_prompt(self, tmp_path):
        """repo_map 生成的内容应出现在发给 LLM 的 system prompt 里。"""
        from agent.core import Agent, AgentConfig
        from agent.event_log import EventLog

        # 在 repo 里放一个 Python 文件，让 repo_map 有内容
        (tmp_path / "mymodule.py").write_text("def my_function():\n    pass\n")

        task = self._make_task(tmp_path)
        registry = ToolRegistry().register(NoopTool("shell"))
        script = [Action(ActionType.FINISH, "done", message="ok")]
        backend = MockBackend(script)
        config = AgentConfig(budget_tokens=80_000)
        agent = Agent(backend, registry, config)

        with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
            agent.run(task, log)

        # 检查 LLM 收到的第一条 system 消息里有 repo 内容
        assert backend.call_count >= 1
        first_messages = backend.received_messages[0]
        system_content = next(
            (m.content for m in first_messages if m.role == "system"), ""
        )
        assert "mymodule.py" in system_content or "my_function" in system_content

    def test_large_history_trimmed(self, tmp_path):
        """历史很长时，TokenBudget 应裁剪而不是崩溃。"""
        from agent.core import Agent, AgentConfig
        from agent.event_log import EventLog

        task = self._make_task(tmp_path)
        registry = ToolRegistry().register(NoopTool("shell"))

        # 造 40 步 tool_call，每步 observation 很长
        class BigOutputTool(NoopTool):
            def execute(self, params):
                from tools.base import ToolResult
                return ToolResult(success=True, output="x" * 2000)

        registry2 = ToolRegistry().register(BigOutputTool("shell"))
        script = [
            Action(ActionType.TOOL_CALL, f"step {i}", ToolCall("shell", {"cmd": f"echo {i}"}))
            for i in range(4)
        ] + [Action(ActionType.FINISH, "done", message="ok")]

        backend = MockBackend(script)
        config = AgentConfig(budget_tokens=10_000, history_max_messages=10)
        agent = Agent(backend, registry2, config)

        with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
            result = agent.run(task, log)

        assert result.is_success()

    def test_long_memory_injected_into_first_prompt(self, tmp_path):
        from agent.core import Agent, AgentConfig
        from agent.event_log import EventLog

        prior_task = Task(
            task_id="prior001",
            description="Fix parser empty input",
            repo_path=str(tmp_path),
            task_category="bugfix",
            expected_failure_type="verification_failed",
            test_cmd="pytest tests/test_parser.py -q",
        )
        append_run_memory(
            tmp_path / "logs",
            prior_task,
            RunResult(
                task_id=prior_task.task_id,
                status=RunStatus.SUCCESS,
                summary="Fixed parser empty input",
                steps_taken=2,
                total_tokens=50,
            ),
        )
        task = Task(
            task_id="ctxmem1",
            description="Fix parser empty input again",
            repo_path=str(tmp_path),
            task_category="bugfix",
            expected_failure_type="verification_failed",
            max_steps=2,
        )
        registry = ToolRegistry().register(NoopTool("shell"))
        backend = MockBackend([Action(ActionType.FINISH, "done", message="ok")])
        config = AgentConfig(
            budget_tokens=80_000,
            log_dir=str(tmp_path / "logs"),
            enable_long_memory=True,
            long_memory_limit=3,
        )
        agent = Agent(backend, registry, config)

        with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
            result = agent.run(task, log)
            events = log.replay()

        assert result.is_success()
        first_messages = backend.received_messages[0]
        assert any("LONG MEMORY" in msg.content for msg in first_messages)
        assert any("Successful patterns for category=bugfix" in msg.content for msg in first_messages)
        assert any(
            event.event_type.value == "reflection"
            and event.payload.get("reason") == "long_memory"
            for event in events
        )

    def test_long_memory_prefers_experience_memory_entries(self, tmp_path):
        prior_task = Task(
            task_id="prior-exp",
            description="Fix parser empty input",
            repo_path=str(tmp_path),
            task_category="bugfix",
            expected_failure_type="verification_failed",
            test_cmd="pytest tests/test_parser.py -q",
        )
        append_run_memory(
            tmp_path / "logs",
            prior_task,
            RunResult(
                task_id=prior_task.task_id,
                status=RunStatus.SUCCESS,
                summary="Fixed parser empty input",
                steps_taken=2,
                total_tokens=50,
            ),
        )

        hits = search_memories(
            tmp_path / "logs",
            query="parser empty input bug",
            repo_path=tmp_path,
            category="bugfix",
            failure_type="verification_failed",
            limit=3,
        )

        assert hits
        assert hits[0].payload.get("memory_kind") == "experience"
