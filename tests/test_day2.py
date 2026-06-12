"""
tests/test_day2.py

覆盖 agent/core.py 的所有关键路径，全程使用 MockBackend，不消耗真实 API。

测试场景：
- 正常完成（FINISH）
- Agent 主动放弃（GIVE_UP）
- 达到步数上限（MAX_STEPS）
- 死循环检测（LOOP_DETECTED）
- Reflection 触发：测试失败
- Reflection 触发：连续 N 步无编辑
- LLM 调用异常
- 工具不存在
- ToolRegistry 基本功能
"""

import subprocess

import pytest

from agent.core import Agent, AgentConfig
from agent.event_log import EventLog
from agent.review import review_patch_metadata
from agent.task import Action, ActionType, RunStatus, Task, ToolCall
from llm.base import MockBackend
from tools.base import FailingTool, NoopTool, ToolRegistry, ToolResult
from tools.file_tool import ApplyPatchTool, FileWriteTool


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def task(tmp_path) -> Task:
    return Task(
        task_id="test0001",
        description="Fix the failing test",
        repo_path=str(tmp_path),
        max_steps=10,
    )


@pytest.fixture
def registry() -> ToolRegistry:
    r = ToolRegistry()
    r.register(NoopTool("shell", output="all tests passed"))
    r.register(NoopTool("file_read", output="def foo(): pass"))
    r.register(NoopTool("file_write", output="file written"))
    return r


@pytest.fixture
def log(tmp_path, task) -> EventLog:
    return EventLog.create(task, log_dir=str(tmp_path / "logs"))


def make_agent(backend, registry=None, config=None) -> Agent:
    if registry is None:
        registry = ToolRegistry()
        registry.register(NoopTool())
    return Agent(backend, registry, config)


def make_tool_call_action(tool="shell", params=None, thought="Let me run the tests.") -> Action:
    return Action(
        action_type=ActionType.TOOL_CALL,
        thought=thought,
        tool_call=ToolCall(name=tool, params=params or {}),
    )


def make_finish_action(message="All done.") -> Action:
    return Action(
        action_type=ActionType.FINISH,
        thought="Task is complete.",
        message=message,
    )


def make_give_up_action(message="Cannot solve.") -> Action:
    return Action(
        action_type=ActionType.GIVE_UP,
        thought="I'm stuck.",
        message=message,
    )


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------

class TestToolRegistry:
    def test_register_and_execute(self):
        tool = NoopTool("mytool", output="hello")
        registry = ToolRegistry()
        registry.register(tool)

        result = registry.execute_tool("mytool", {})
        assert result.success
        assert result.output == "hello"

    def test_duplicate_register_raises(self):
        registry = ToolRegistry()
        registry.register(NoopTool("mytool"))
        with pytest.raises(ValueError, match="already registered"):
            registry.register(NoopTool("mytool"))

    def test_unknown_tool_returns_error(self):
        registry = ToolRegistry()
        result = registry.execute_tool("nonexistent", {})
        assert not result.success
        assert "nonexistent" in result.error

    def test_get_schemas_returns_all(self):
        registry = ToolRegistry()
        registry.register(NoopTool("a"))
        registry.register(NoopTool("b"))
        schemas = registry.get_schemas()
        names = [s.name for s in schemas]
        assert "a" in names
        assert "b" in names

    def test_chain_register(self):
        registry = (
            ToolRegistry()
            .register(NoopTool("x"))
            .register(NoopTool("y"))
        )
        assert len(registry) == 2

    def test_contains(self):
        registry = ToolRegistry()
        registry.register(NoopTool("z"))
        assert "z" in registry
        assert "w" not in registry

    def test_tool_exception_returns_error(self):
        """工具内部抛异常时，registry 把它包成 error result，不向上传播。"""
        class BrokenTool(NoopTool):
            def execute(self, params):
                raise RuntimeError("disk full")

        registry = ToolRegistry()
        registry.register(BrokenTool("broken"))
        result = registry.execute_tool("broken", {})
        assert not result.success
        assert "disk full" in result.error


# ---------------------------------------------------------------------------
# MockBackend
# ---------------------------------------------------------------------------

class TestMockBackend:
    def test_returns_scripted_actions_in_order(self):
        actions = [make_tool_call_action(), make_finish_action()]
        backend = MockBackend(actions)

        r1 = backend.complete([], [])
        r2 = backend.complete([], [])
        assert r1.action.action_type == ActionType.TOOL_CALL
        assert r2.action.action_type == ActionType.FINISH

    def test_script_exhausted_returns_give_up(self):
        backend = MockBackend([make_finish_action()])
        backend.complete([], [])   # 用完脚本
        r = backend.complete([], [])
        assert r.action.action_type == ActionType.GIVE_UP

    def test_tracks_call_count(self):
        backend = MockBackend([make_finish_action(), make_finish_action()])
        backend.complete([], [])
        backend.complete([], [])
        assert backend.call_count == 2

    def test_reset(self):
        backend = MockBackend([make_finish_action()])
        backend.complete([], [])
        backend.reset()
        r = backend.complete([], [])
        assert r.action.action_type == ActionType.FINISH


# ---------------------------------------------------------------------------
# Agent.run — 正常完成
# ---------------------------------------------------------------------------

class TestAgentFinish:
    def test_finish_on_first_action(self, task, log, registry):
        backend = MockBackend([make_finish_action("Fixed it!")])
        agent = Agent(backend, registry)

        result = agent.run(task, log)

        assert result.status == RunStatus.SUCCESS
        assert result.summary == "Fixed it!"
        assert result.steps_taken == 1
        assert result.total_tokens > 0

    def test_finish_after_tool_calls(self, task, log, registry):
        script = [
            make_tool_call_action("shell"),
            make_tool_call_action("file_read"),
            make_finish_action("Done after exploration."),
        ]
        backend = MockBackend(script)
        agent = Agent(backend, registry)

        result = agent.run(task, log)

        assert result.status == RunStatus.SUCCESS
        assert result.steps_taken == 3

    def test_event_log_records_correct_sequence(self, task, log, registry):
        from agent.task import EventType

        script = [make_tool_call_action("shell"), make_finish_action()]
        backend = MockBackend(script)
        agent = Agent(backend, registry)
        agent.run(task, log)

        events = log.replay()

    def test_finish_early_when_targeted_test_already_passes_without_edits(self, tmp_path):
        from agent.task import EventType

        task = Task(
            task_id="verified1",
            description="Task already fixed",
            repo_path=str(tmp_path),
            test_cmd="python -m pytest test_demo.py -q",
            finish_if_verified=True,
            max_steps=5,
        )
        registry = ToolRegistry().register(NoopTool("test", output="1 passed"))
        backend = MockBackend([
            make_tool_call_action("test", {"path": "test_demo.py"}),
            make_tool_call_action("file_read", {"path": "later.py"}),
        ])
        agent = Agent(backend, registry)

        with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
            result = agent.run(task, log)
            events = log.replay()

        assert result.status == RunStatus.SUCCESS
        assert result.steps_taken == 1
        assert "already satisfies" in result.summary
        types = [e.event_type for e in events]

        assert types[0] == EventType.TASK_START
        assert EventType.ACTION in types
        assert EventType.OBSERVATION in types
        assert types[-1] == EventType.TASK_COMPLETE


# ---------------------------------------------------------------------------
# Agent.run — 主动放弃
# ---------------------------------------------------------------------------

class TestAgentGiveUp:
    def test_give_up_returns_gave_up_status(self, task, log, registry):
        backend = MockBackend([make_give_up_action("Too complex.")])
        agent = Agent(backend, registry)

        result = agent.run(task, log)

        assert result.status == RunStatus.GAVE_UP
        assert result.summary == "Too complex."

    def test_give_up_event_logged(self, task, log, registry):
        from agent.task import EventType

        backend = MockBackend([make_give_up_action()])
        agent = Agent(backend, registry)
        agent.run(task, log)

        events = log.replay()
        assert events[-1].event_type == EventType.TASK_FAILED


# ---------------------------------------------------------------------------
# Agent.run — 超出步数上限
# ---------------------------------------------------------------------------

class TestAgentMaxSteps:
    def test_max_steps_returns_correct_status(self, tmp_path):
        task = Task(
            task_id="maxtest",
            description="run forever",
            repo_path=str(tmp_path),
            max_steps=3,
        )
        log = EventLog.create(task, log_dir=str(tmp_path / "logs"))
        # 每步 cmd 不同避免触发 loop detection；max_steps=3 先到
        script = [
            make_tool_call_action("shell", {"cmd": f"echo {i}"})
            for i in range(10)
        ]
        backend = MockBackend(script)
        registry = ToolRegistry()
        registry.register(NoopTool("shell"))
        config = AgentConfig(loop_detection_window=10)
        agent = Agent(backend, registry, config)

        result = agent.run(task, log)

        assert result.status == RunStatus.MAX_STEPS
        assert result.steps_taken == 3
        log.close()

    def test_max_steps_event_logged(self, tmp_path):
        from agent.task import EventType

        task = Task(
            task_id="maxtest2",
            description="run forever",
            repo_path=str(tmp_path),
            max_steps=2,
        )
        log = EventLog.create(task, log_dir=str(tmp_path / "logs"))
        backend = MockBackend([make_tool_call_action()] * 10)
        registry = ToolRegistry()
        registry.register(NoopTool())
        agent = Agent(backend, registry)
        agent.run(task, log)

        events = log.replay()
        assert events[-1].event_type == EventType.TASK_FAILED
        assert "max_steps" in events[-1].payload["reason"]
        log.close()


# ---------------------------------------------------------------------------
# Agent.run — 死循环检测
# ---------------------------------------------------------------------------

class TestLoopDetection:
    def test_loop_detected_gives_up(self, tmp_path):
        task = Task(
            task_id="looptest",
            description="infinite loop",
            repo_path=str(tmp_path),
            max_steps=20,
        )
        log = EventLog.create(task, log_dir=str(tmp_path / "logs"))
        # 同一个 action 重复 N 次
        repeated = make_tool_call_action("shell", {"cmd": "echo hi"})
        backend = MockBackend([repeated] * 20)
        registry = ToolRegistry()
        registry.register(NoopTool("shell"))
        config = AgentConfig(loop_detection_window=3)
        agent = Agent(backend, registry, config)

        result = agent.run(task, log)

        assert result.status == RunStatus.GAVE_UP
        assert "Loop detected" in result.summary
        log.close()

    def test_different_actions_not_detected_as_loop(self, task, log, registry):
        """不同 action 交替出现，不应触发死循环。"""
        script = [
            make_tool_call_action("shell", {"cmd": "pytest"}),
            make_tool_call_action("file_read", {"path": "foo.py"}),
            make_tool_call_action("shell", {"cmd": "pytest"}),
            make_finish_action(),
        ]
        backend = MockBackend(script)
        config = AgentConfig(loop_detection_window=3)
        agent = Agent(backend, registry, config)

        result = agent.run(task, log)
        assert result.status == RunStatus.SUCCESS


# ---------------------------------------------------------------------------
# Agent.run — Reflection：测试失败
# ---------------------------------------------------------------------------

class TestReflectionTestFailed:
    def test_reflection_triggered_on_test_failure(self, tmp_path):
        from agent.task import EventType

        task = Task(
            task_id="refltest",
            description="fix tests",
            repo_path=str(tmp_path),
            max_steps=10,
        )
        log = EventLog.create(task, log_dir=str(tmp_path / "logs"))

        # FailingTool 模拟 pytest 失败
        registry = ToolRegistry()
        registry.register(FailingTool("test", "AssertionError: 1 != 2"))
        registry.register(NoopTool("file_write"))

        script = [
            make_tool_call_action("test"),    # 触发 reflection
            make_tool_call_action("file_write"),
            make_finish_action(),
        ]
        backend = MockBackend(script)
        config = AgentConfig(test_tool_names=("test",))
        agent = Agent(backend, registry, config)

        agent.run(task, log)

        events = log.replay()
        reflection_events = [e for e in events if e.event_type == EventType.REFLECTION]
        assert len(reflection_events) >= 1
        assert any(e.payload["reason"] == "test_failed" for e in reflection_events)
        log.close()

    def test_reflection_injected_into_history(self, tmp_path):
        """Reflection 后，LLM 收到的下一条 messages 应包含 reflection prompt。"""
        task = Task(
            task_id="reflhist",
            description="fix tests",
            repo_path=str(tmp_path),
            max_steps=10,
        )
        log = EventLog.create(task, log_dir=str(tmp_path / "logs"))

        registry = ToolRegistry()
        registry.register(FailingTool("test"))
        registry.register(NoopTool("file_write"))
        graph_tool = NoopTool("graph_neighbors", output="neighbors: report.py -> scores.py")
        registry.register(graph_tool)

        script = [
            make_tool_call_action("test"),
            make_finish_action(),
        ]
        backend = MockBackend(script)
        config = AgentConfig(test_tool_names=("test",))
        agent = Agent(backend, registry, config)
        agent.run(task, log)

        # 第二次调用 LLM 时收到的 messages 应包含 REFLECTION 字样
        assert backend.call_count >= 2
        second_call_messages = backend.received_messages[1]
        contents = " ".join(m.content for m in second_call_messages)
        assert "REFLECTION" in contents
        log.close()

    def test_test_failure_triggers_auto_symbol_probe(self, tmp_path):
        task = Task(
            task_id="reflsymbol",
            description="fix tests",
            repo_path=str(tmp_path),
            max_steps=5,
        )
        log = EventLog.create(task, log_dir=str(tmp_path / "logs"))
        registry = ToolRegistry()
        registry.register(FailingTool("test", "FAILED test_demo.py::test_build"))
        symbol_tool = NoopTool("find_symbol", output="test_demo.py:1: def test_build")
        registry.register(symbol_tool)
        script = [
            make_tool_call_action("test"),
            make_finish_action(),
        ]
        backend = MockBackend(script)
        agent = Agent(backend, registry)

        agent.run(task, log)

        assert symbol_tool.call_count == 1
        events = log.replay()
        assert any(
            e.event_type.value == "observation"
            and e.payload["observation"]["metadata"].get("auto_symbol_probe")
            for e in events
        )
        log.close()


class SequencedTool(NoopTool):
    def __init__(self, tool_name, results):
        super().__init__(tool_name)
        self._results = list(results)

    def execute(self, params):
        self.call_count += 1
        self.last_params = params
        if self._results:
            return self._results.pop(0)
        return ToolResult(success=True, output="ok")


class TestFinishVerification:
    def test_failure_analysis_and_hybrid_retrieval_are_injected(self, tmp_path):
        task = Task(
            task_id="analysis1",
            description="fix parser empty input",
            repo_path=str(tmp_path),
            target_files=["parser.py"],
            max_steps=3,
        )
        (tmp_path / "parser.py").write_text("def parse_empty(value):\n    return value.strip()\n", encoding="utf-8")
        (tmp_path / "test_parser.py").write_text("from parser import parse_empty\n", encoding="utf-8")
        log = EventLog.create(task, log_dir=str(tmp_path / "logs"))
        registry = ToolRegistry()
        registry.register(FailingTool("test", 'FAILED test_parser.py::test_empty\nFile "parser.py", line 1\nValueError: bad empty'))
        script = [
            make_tool_call_action("test", {"path": "test_parser.py"}),
            make_give_up_action("stop"),
        ]
        backend = MockBackend(script)
        config = AgentConfig(test_tool_names=("test",), auto_graph_probe_on_test_failure=False)
        agent = Agent(backend, registry, config)

        agent.run(task, log)

        second_call_messages = backend.received_messages[1]
        contents = "\n".join(message.content for message in second_call_messages)
        assert "[FAILURE ANALYSIS]" in contents
        assert "test_parser.py::test_empty" in contents
        assert "[HYBRID RETRIEVAL]" in contents
        assert "parser.py" in contents

    def test_edit_plan_recorded_for_patch(self, tmp_path):
        task = Task(task_id="plan1", description="edit demo", repo_path=str(tmp_path), max_steps=3)
        target = tmp_path / "demo.py"
        target.write_text("x = 1\n", encoding="utf-8")
        log = EventLog.create(task, log_dir=str(tmp_path / "logs"))
        registry = ToolRegistry()
        registry.register(ApplyPatchTool())
        script = [
            make_tool_call_action(
                "apply_patch",
                {
                    "patch_type": "search_replace",
                    "path": str(target),
                    "search": "x = 1",
                    "replace": "x = 2",
                },
                thought='EDIT_PLAN: {"target_files":["demo.py"],"change_intent":"update x","expected_behavior":"x changes","risk_level":"low","tests_to_run":[]}',
            ),
            make_finish_action("done"),
        ]
        backend = MockBackend(script)
        agent = Agent(backend, registry)

        result = agent.run(task, log)

        assert result.is_success()
        events = log.replay()
        assert any(e.event_type.value == "reflection" and e.payload["reason"] == "edit_plan" for e in events)
        patch_obs = [
            e.payload["observation"] for e in events
            if e.event_type.value == "observation" and e.payload["observation"]["tool_name"] == "apply_patch"
        ][0]
        assert patch_obs["metadata"]["edit_plan"]["change_intent"] == "update x"

    def test_markdown_edit_plan_is_parsed_for_patch(self, tmp_path):
        task = Task(task_id="planmd", description="edit demo", repo_path=str(tmp_path), max_steps=3)
        target = tmp_path / "demo.py"
        target.write_text("x = 1\n", encoding="utf-8")
        log = EventLog.create(task, log_dir=str(tmp_path / "logs"))
        registry = ToolRegistry()
        registry.register(ApplyPatchTool())
        script = [
            make_tool_call_action(
                "apply_patch",
                {
                    "patch_type": "search_replace",
                    "path": str(target),
                    "search": "x = 1",
                    "replace": "x = 2",
                },
                thought='**EDIT_PLAN:**\n```json\n{"target_files":["demo.py"],"change_intent":"update x","expected_behavior":"x changes","risk_level":"low","tests_to_run":[]}\n```',
            ),
            make_finish_action("done"),
        ]
        backend = MockBackend(script)
        agent = Agent(backend, registry)

        result = agent.run(task, log)

        assert result.is_success()
        patch_obs = [
            e.payload["observation"] for e in log.replay()
            if e.event_type.value == "observation" and e.payload["observation"]["tool_name"] == "apply_patch"
        ][0]
        assert patch_obs["metadata"]["edit_plan"]["source"] == "model"
        assert patch_obs["metadata"]["edit_plan"]["change_intent"] == "update x"

    def test_invalid_edit_plan_falls_back_to_patch_path(self, tmp_path):
        task = Task(task_id="planfallback", description="edit demo", repo_path=str(tmp_path), max_steps=3)
        target = tmp_path / "demo.py"
        target.write_text("x = 1\n", encoding="utf-8")
        log = EventLog.create(task, log_dir=str(tmp_path / "logs"))
        registry = ToolRegistry()
        registry.register(ApplyPatchTool())
        script = [
            make_tool_call_action(
                "apply_patch",
                {
                    "patch_type": "search_replace",
                    "path": str(target),
                    "search": "x = 1",
                    "replace": "x = 2",
                },
                thought="EDIT_PLAN: this is not json",
            ),
            make_finish_action("done"),
        ]
        backend = MockBackend(script)
        agent = Agent(backend, registry)

        result = agent.run(task, log)

        assert result.is_success()
        assert target.read_text(encoding="utf-8") == "x = 2\n"
        patch_obs = [
            e.payload["observation"] for e in log.replay()
            if e.event_type.value == "observation" and e.payload["observation"]["tool_name"] == "apply_patch"
        ][0]
        assert patch_obs["metadata"]["edit_plan"]["source"] == "inferred_after_invalid"

    def test_benchmark_verify_task_runs_target_verification(self, tmp_path):
        task = Task(
            task_id="scopeguard",
            description="fix one test",
            repo_path=str(tmp_path),
            test_cmd="python -m pytest test_demo.py::test_target -q",
            max_steps=3,
        )
        log = EventLog.create(task, log_dir=str(tmp_path / "logs"))
        registry = ToolRegistry()
        registry.register(NoopTool("shell", output="1 passed in 0.01s"))
        script = [
            make_tool_call_action(
                "verify_task",
                {},
                thought="Run the benchmark verification.",
            ),
            make_finish_action("done"),
        ]
        backend = MockBackend(script)
        agent = Agent(backend, registry, AgentConfig(run_mode="benchmark"))

        result = agent.run(task, log)

        assert result.is_success()
        verify_obs = [
            e.payload["observation"] for e in log.replay()
            if e.event_type.value == "observation" and e.payload["observation"]["tool_name"] == "verify_task"
        ][0]
        assert verify_obs["metadata"]["verification_scope_guard"] is True
        assert verify_obs["metadata"]["benchmark_verify_tool"] is True
        assert "test_demo.py::test_target" in verify_obs["output"]

    def test_benchmark_scope_guard_blocks_broad_test_tool_calls(self, tmp_path):
        task = Task(
            task_id="scopeguarddir",
            description="fix one test",
            repo_path=str(tmp_path),
            test_cmd="python -m pytest test_demo.py::test_target -q",
            max_steps=3,
        )
        log = EventLog.create(task, log_dir=str(tmp_path / "logs"))
        registry = ToolRegistry()
        registry.register(NoopTool("shell", output="1 passed in 0.01s"))
        registry.register(FailingTool("test", "full suite should not run"))
        script = [
            make_tool_call_action(
                "test",
                {"path": str(tmp_path)},
                thought="Run all tests in the workspace.",
            ),
            make_tool_call_action("verify_task", {}, thought="Use the benchmark verification tool instead."),
            make_finish_action("done"),
        ]
        backend = MockBackend(script)
        agent = Agent(backend, registry, AgentConfig(run_mode="benchmark"))

        result = agent.run(task, log)

        assert result.is_success()
        test_obs = [
            e.payload["observation"] for e in log.replay()
            if e.event_type.value == "observation" and e.payload["observation"]["tool_name"] == "test"
        ][0]
        assert test_obs["metadata"]["verification_scope_guard"] is True
        assert test_obs["metadata"]["verification_scope_rejected"] is True
        assert "use verify_task" in test_obs["error"].lower()

    def test_benchmark_mode_skips_git_write_tools(self, tmp_path):
        task = Task(
            task_id="gitguard",
            description="fix one test",
            repo_path=str(tmp_path),
            max_steps=3,
        )
        log = EventLog.create(task, log_dir=str(tmp_path / "logs"))
        registry = ToolRegistry()
        registry.register(FailingTool("git_add", "git add should not run"))
        script = [
            make_tool_call_action("git_add", {"paths": ["."]}, thought="Stage changes."),
            make_finish_action("done"),
        ]
        backend = MockBackend(script)
        agent = Agent(backend, registry, AgentConfig(run_mode="benchmark"))

        result = agent.run(task, log)

        assert result.is_success()
        git_obs = [
            e.payload["observation"] for e in log.replay()
            if e.event_type.value == "observation" and e.payload["observation"]["tool_name"] == "git_add"
        ][0]
        assert git_obs["metadata"]["benchmark_git_write_guard"] is True
        assert "Skipped git write operation" in git_obs["output"]

    def test_benchmark_shell_git_diff_is_workspace_scoped(self, tmp_path):
        task = Task(
            task_id="shellgitguard",
            description="inspect diff",
            repo_path=str(tmp_path),
            max_steps=3,
        )
        log = EventLog.create(task, log_dir=str(tmp_path / "logs"))
        registry = ToolRegistry()
        registry.register(NoopTool("shell", output="No unstaged changes."))
        script = [
            make_tool_call_action(
                "shell",
                {"cmd": "cd /parent/repo/logs/workspaces/case1 && git diff"},
                thought="Inspect diff.",
            ),
            make_finish_action("done"),
        ]
        backend = MockBackend(script)
        agent = Agent(backend, registry, AgentConfig(run_mode="benchmark"))

        result = agent.run(task, log)

        assert result.is_success()
        shell_obs = [
            e.payload["observation"] for e in log.replay()
            if e.event_type.value == "observation" and e.payload["observation"]["tool_name"] == "shell"
        ][0]
        assert shell_obs["metadata"]["shell_git_scope_guard"] is True
        assert shell_obs["metadata"]["guarded_cmd"] == "git diff -- ."
        assert "GIT SCOPE GUARD" in shell_obs["output"]

    def test_benchmark_test_tool_defaults_to_task_repo_cwd(self, tmp_path):
        task = Task(
            task_id="testcwd",
            description="run targeted test",
            repo_path=str(tmp_path),
            test_cmd="python -m pytest test_demo.py::test_target -q",
            max_steps=3,
        )
        log = EventLog.create(task, log_dir=str(tmp_path / "logs"))
        registry = ToolRegistry()
        registry.register(NoopTool("test", output="1 passed"))
        script = [
            make_tool_call_action(
                "test",
                {"path": "test_demo.py::test_target"},
                thought="Run targeted test.",
            ),
            make_finish_action("done"),
        ]
        backend = MockBackend(script)
        agent = Agent(backend, registry, AgentConfig(run_mode="benchmark"))

        result = agent.run(task, log)

        assert result.is_success()
        assert script[0].tool_call.params["cwd"] == str(tmp_path)
        assert script[0].tool_call.params["benchmark_default_cwd"] is True

    def test_benchmark_auto_finishes_after_patch_and_target_verification(self, tmp_path):
        target = tmp_path / "demo.py"
        target.write_text("x = 1\n", encoding="utf-8")
        task = Task(
            task_id="autofinish",
            description="fix x",
            repo_path=str(tmp_path),
            test_cmd="python -m pytest test_demo.py::test_x -q",
            finish_if_verified=True,
            max_steps=5,
        )
        log = EventLog.create(task, log_dir=str(tmp_path / "logs"))
        registry = ToolRegistry()
        registry.register(ApplyPatchTool())
        registry.register(NoopTool("test", output="1 passed"))
        script = [
            make_tool_call_action(
                "apply_patch",
                {
                    "patch_type": "search_replace",
                    "path": str(target),
                    "search": "x = 1",
                    "replace": "x = 2",
                },
                thought='EDIT_PLAN: {"target_files":["demo.py"],"change_intent":"update x","expected_behavior":"test passes","risk_level":"low","tests_to_run":["python -m pytest test_demo.py::test_x -q"]}',
            ),
            make_tool_call_action(
                "test",
                {"path": "test_demo.py::test_x"},
                thought="Verify targeted test.",
            ),
            make_give_up_action("Model stopped with no content"),
        ]
        backend = MockBackend(script)
        agent = Agent(backend, registry, AgentConfig(run_mode="benchmark"))

        result = agent.run(task, log)

        assert result.is_success()
        assert result.steps_taken == 2
        assert "auto-finished" in result.summary

    def test_auto_finish_after_patch_is_benchmark_only(self, tmp_path):
        target = tmp_path / "demo.py"
        target.write_text("x = 1\n", encoding="utf-8")
        task = Task(
            task_id="noautofinish",
            description="fix x",
            repo_path=str(tmp_path),
            test_cmd="python -m pytest test_demo.py::test_x -q",
            finish_if_verified=True,
            max_steps=5,
        )
        log = EventLog.create(task, log_dir=str(tmp_path / "logs"))
        registry = ToolRegistry()
        registry.register(ApplyPatchTool())
        registry.register(NoopTool("test", output="1 passed"))
        script = [
            make_tool_call_action(
                "apply_patch",
                {
                    "patch_type": "search_replace",
                    "path": str(target),
                    "search": "x = 1",
                    "replace": "x = 2",
                },
                thought='EDIT_PLAN: {"target_files":["demo.py"],"change_intent":"update x","expected_behavior":"test passes","risk_level":"low","tests_to_run":["python -m pytest test_demo.py::test_x -q"]}',
            ),
            make_tool_call_action(
                "test",
                {"path": "test_demo.py::test_x", "cwd": str(tmp_path)},
                thought="Verify targeted test.",
            ),
            make_give_up_action("still not done"),
        ]
        backend = MockBackend(script)
        agent = Agent(backend, registry, AgentConfig(run_mode="auto"))

        result = agent.run(task, log)

        assert result.status == RunStatus.GAVE_UP
        assert result.summary == "still not done"

    def test_finish_diff_does_not_include_dirty_parent_repo(self, tmp_path):
        parent = tmp_path / "repo"
        parent.mkdir()
        workspace = parent / "logs" / "workspaces" / "case1"
        workspace.mkdir(parents=True)
        tracked = parent / "tracked.py"
        tracked.write_text("value = 1\n", encoding="utf-8")
        (workspace / "demo.py").write_text("demo = 1\n", encoding="utf-8")
        subprocess.run(["git", "init"], cwd=parent, check=True, capture_output=True, text=True)
        subprocess.run(["git", "add", "tracked.py"], cwd=parent, check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=parent,
            check=True,
            capture_output=True,
            text=True,
            env={
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "test@example.com",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "test@example.com",
            },
        )
        tracked.write_text("value = 2\n", encoding="utf-8")

        task = Task(task_id="diffscope", description="finish", repo_path=str(workspace), max_steps=1)
        log = EventLog.create(task, log_dir=str(tmp_path / "logs"))
        agent = Agent(MockBackend([make_finish_action("done")]), ToolRegistry())

        result = agent.run(task, log)

        assert result.is_success()
        assert result.patch is None or "tracked.py" not in result.patch

    def test_safe_mode_blocks_patch_before_write(self, tmp_path):
        task = Task(task_id="safe1", description="do not write", repo_path=str(tmp_path), max_steps=2)
        target = tmp_path / "demo.py"
        target.write_text("x = 1\n", encoding="utf-8")
        log = EventLog.create(task, log_dir=str(tmp_path / "logs"))
        registry = ToolRegistry()
        registry.register(ApplyPatchTool())
        script = [
            make_tool_call_action(
                "apply_patch",
                {"patch_type": "replace_file", "path": str(target), "content": "x = 2\n"},
            ),
            make_give_up_action("blocked"),
        ]
        backend = MockBackend(script)
        agent = Agent(backend, registry, AgentConfig(run_mode="safe"))

        result = agent.run(task, log)

        assert result.status == RunStatus.GAVE_UP
        assert target.read_text(encoding="utf-8") == "x = 1\n"
        events = log.replay()
        obs = [
            e.payload["observation"] for e in events
            if e.event_type.value == "observation" and e.payload["observation"]["tool_name"] == "apply_patch"
        ][0]
        assert obs["metadata"]["run_mode"] == "safe"

    def test_patch_review_blocks_high_risk_finish(self, tmp_path):
        task = Task(task_id="patchreview", description="avoid tests", repo_path=str(tmp_path), max_steps=5)
        target = tmp_path / "test_demo.py"
        target.write_text("def test_x():\n    assert False\n", encoding="utf-8")
        log = EventLog.create(task, log_dir=str(tmp_path / "logs"))
        registry = ToolRegistry()
        registry.register(ApplyPatchTool())
        script = [
            make_tool_call_action(
                "apply_patch",
                {
                    "patch_type": "replace_file",
                    "path": str(target),
                    "content": "def test_x():\n    assert True\n",
                },
            ),
            make_finish_action("too risky"),
            make_give_up_action("blocked by review"),
        ]
        backend = MockBackend(script)
        agent = Agent(backend, registry)

        result = agent.run(task, log)

        assert result.status == RunStatus.GAVE_UP
        events = log.replay()
        assert any(
            e.event_type.value == "reflection" and e.payload["reason"] == "self_review_failed"
            for e in events
        )

    def test_patch_review_flags_edit_plan_path_mismatch(self):
        review = review_patch_metadata(
            {
                "patch": {"patch_type": "replace_file", "path": "src/actual.py", "content": "x = 1\n"},
                "edit_plan": {
                    "target_files": ["src/expected.py"],
                    "risk_level": "low",
                    "change_intent": "update expected",
                },
                "line_count": 1,
                "stats": {"operation": "replace_file"},
            }
        )

        assert review.success is False
        assert review.risk_level == "high"
        assert any("EDIT_PLAN targeted" in item for item in review.findings)

    def test_file_write_exposes_patch_metadata_for_review(self, tmp_path):
        tool = FileWriteTool()
        target = tmp_path / "demo.py"

        result = tool.execute({"path": str(target), "content": "x = 1\n"})

        assert result.success is True
        assert result.metadata["patch"]["path"] == str(target)
        assert result.metadata["patch"]["patch_type"] == "replace_file"
        assert result.metadata["stats"]["operation"] == "replace_file"

    def test_finish_verification_failure_feeds_back_into_next_round(self, tmp_path):
        task = Task(
            task_id="verifyloop",
            description="fix tests",
            repo_path=str(tmp_path),
            test_cmd="python -m pytest test_demo.py -q",
            max_steps=5,
        )
        log = EventLog.create(task, log_dir=str(tmp_path / "logs"))
        registry = ToolRegistry()
        registry.register(NoopTool("file_write", output="file written"))
        registry.register(SequencedTool("shell", [
            ToolResult(success=False, output="FAILED test_demo.py::test_x", error="Exit code: 1", failure_type="verification_failed"),
            ToolResult(success=True, output="1 passed"),
        ]))
        script = [
            make_tool_call_action("file_write", {"path": "demo.py", "content": "x=1"}),
            make_finish_action("done too early"),
            make_tool_call_action("file_write", {"path": "demo.py", "content": "x=2"}),
            make_finish_action("done after fix"),
        ]
        backend = MockBackend(script)
        agent = Agent(backend, registry)

        result = agent.run(task, log)

        assert result.status == RunStatus.SUCCESS
        assert result.summary == "done after fix"
        events = log.replay()
        assert any(
            e.event_type.value == "reflection"
            and e.payload["reason"] == "finish_verification_failed"
            for e in events
        )
        assert backend.call_count >= 4

    def test_self_review_blocks_unresolved_conflict_marker(self, tmp_path):
        task = Task(
            task_id="reviewfail",
            description="fix conflict marker",
            repo_path=str(tmp_path),
            max_steps=5,
        )
        target = tmp_path / "demo.py"
        target.write_text("base\n", encoding="utf-8")
        log = EventLog.create(task, log_dir=str(tmp_path / "logs"))
        registry = ToolRegistry()
        registry.register(ApplyPatchTool())
        script = [
            make_tool_call_action(
                "apply_patch",
                {
                    "patch_type": "replace_file",
                    "path": str(target),
                    "content": "<<<<<<< HEAD\nbad\n>>>>>>> branch\n",
                },
            ),
            make_finish_action("done too early"),
            make_tool_call_action(
                "apply_patch",
                {
                    "patch_type": "replace_file",
                    "path": str(target),
                    "content": "fixed\n",
                },
            ),
            make_finish_action("done after review"),
        ]
        backend = MockBackend(script)
        agent = Agent(backend, registry)

        result = agent.run(task, log)

        assert result.status == RunStatus.SUCCESS
        assert result.summary == "done after review"
        events = log.replay()
        assert any(
            e.event_type.value == "reflection"
            and e.payload["reason"] == "self_review_failed"
            for e in events
        )

    def test_taxonomy_recovery_prompt_after_patch_conflict(self, tmp_path):
        task = Task(
            task_id="recoverpatch",
            description="recover from conflict",
            repo_path=str(tmp_path),
            max_steps=4,
        )
        target = tmp_path / "demo.py"
        target.write_text("current\n", encoding="utf-8")
        log = EventLog.create(task, log_dir=str(tmp_path / "logs"))
        registry = ToolRegistry()
        registry.register(ApplyPatchTool())
        script = [
            make_tool_call_action(
                "apply_patch",
                {
                    "patch_type": "replace_file",
                    "path": str(target),
                    "content": "new\n",
                    "expected_content": "stale\n",
                },
            ),
            make_give_up_action("cannot patch"),
        ]
        backend = MockBackend(script)
        agent = Agent(backend, registry)

        result = agent.run(task, log)

        assert result.failure_type == "patch_conflict"
        second_call_messages = backend.received_messages[1]
        contents = " ".join(m.content for m in second_call_messages)
        assert "Read the target file again" in contents

    def test_reflection_includes_graph_hint_for_targeted_test(self, tmp_path):
        task = Task(
            task_id="reflgraph",
            description="fix tests",
            repo_path=str(tmp_path),
            test_cmd="python -m pytest test_report.py -q",
            target_files=["report.py"],
            max_steps=10,
        )
        (tmp_path / "scores.py").write_text("def normalize_subject(name):\n    return name.lower()\n")
        (tmp_path / "report.py").write_text("from scores import normalize_subject\n")
        (tmp_path / "test_report.py").write_text("from report import build_report\n")
        log = EventLog.create(task, log_dir=str(tmp_path / "logs"))

        registry = ToolRegistry()
        registry.register(FailingTool("test"))
        registry.register(NoopTool("file_write"))
        file_read_tool = NoopTool("file_read", output="File: report.py\n   1 | from scores import normalize_subject")
        registry.register(file_read_tool)
        graph_tool = NoopTool("graph_neighbors", output="neighbors: report.py -> scores.py")
        registry.register(graph_tool)

        script = [
            make_tool_call_action("test", {"path": "test_report.py"}),
            make_finish_action(),
        ]
        backend = MockBackend(script)
        config = AgentConfig(test_tool_names=("test",))
        agent = Agent(backend, registry, config)
        agent.run(task, log)

        second_call_messages = backend.received_messages[1]
        contents = "\n".join(m.content for m in second_call_messages)
        assert graph_tool.call_count >= 1
        assert file_read_tool.call_count >= 1
        assert file_read_tool.last_params is not None
        assert file_read_tool.last_params["path"].endswith("report.py")
        assert graph_tool.last_params is not None
        assert graph_tool.last_params["path"] in {"report.py", "scores.py"}
        assert "[Tool: file_read | SUCCESS]" in contents
        assert "[Tool: graph_neighbors | SUCCESS]" in contents
        assert "[GRAPH HINT]" in contents
        assert "test_report.py" in contents
        assert "report.py" in contents
        assert "1. report.py" in contents
        assert "scores.py" in contents
        log.close()


# ---------------------------------------------------------------------------
# Agent.run — Reflection：连续 N 步无编辑
# ---------------------------------------------------------------------------

class TestReflectionNoEdit:
    def test_reflection_triggered_after_no_edit_steps(self, tmp_path):
        from agent.task import EventType

        task = Task(
            task_id="noedit",
            description="explore forever",
            repo_path=str(tmp_path),
            max_steps=20,
        )
        log = EventLog.create(task, log_dir=str(tmp_path / "logs"))

        registry = ToolRegistry()
        registry.register(NoopTool("shell"))
        registry.register(NoopTool("file_read"))

        # 每步 cmd 不同避免触发 loop detection；连续 6 步无 file_write 触发 no_edit
        script = [
            make_tool_call_action("shell", {"cmd": f"echo {i}"})
            for i in range(6)
        ] + [make_finish_action()]
        backend = MockBackend(script)
        config = AgentConfig(reflection_no_edit_steps=6, loop_detection_window=10)
        agent = Agent(backend, registry, config)

        agent.run(task, log)

        events = log.replay()
        reflection_events = [e for e in events if e.event_type == EventType.REFLECTION]
        no_edit_reflections = [e for e in reflection_events if e.payload["reason"] == "no_edit"]
        assert len(no_edit_reflections) >= 1
        log.close()

    def test_apply_patch_counts_as_edit(self, tmp_path):
        from agent.task import EventType

        task = Task(
            task_id="patchedit",
            description="edit via patch",
            repo_path=str(tmp_path),
            max_steps=10,
        )
        log = EventLog.create(task, log_dir=str(tmp_path / "logs"))

        registry = ToolRegistry()
        registry.register(NoopTool("shell"))
        registry.register(NoopTool("apply_patch"))

        script = [
            make_tool_call_action("shell", {"cmd": "echo 1"}),
            make_tool_call_action("shell", {"cmd": "echo 2"}),
            make_tool_call_action("apply_patch", {"patch_type": "replace_file", "path": "a.py", "content": "x = 1\n"}),
            make_tool_call_action("shell", {"cmd": "echo 3"}),
            make_tool_call_action("shell", {"cmd": "echo 4"}),
            make_finish_action(),
        ]
        backend = MockBackend(script)
        config = AgentConfig(reflection_no_edit_steps=3, loop_detection_window=10)
        agent = Agent(backend, registry, config)

        agent.run(task, log)

        events = log.replay()
        reflection_events = [e for e in events if e.event_type == EventType.REFLECTION]
        no_edit_reflections = [e for e in reflection_events if e.payload["reason"] == "no_edit"]
        assert len(no_edit_reflections) == 0
        log.close()


# ---------------------------------------------------------------------------
# Agent.run — LLM 调用异常
# ---------------------------------------------------------------------------

class TestLLMError:
    def test_llm_exception_returns_failed(self, task, log, registry):
        class CrashingBackend(MockBackend):
            def complete(self, messages, tools):
                raise ConnectionError("API unreachable")

        agent = Agent(CrashingBackend([]), registry)
        result = agent.run(task, log)

        assert result.status == RunStatus.FAILED
        assert "API unreachable" in result.error
        assert result.failure_type == "llm_error"

    def test_llm_error_logged(self, task, log, registry):
        from agent.task import EventType

        class CrashingBackend(MockBackend):
            def complete(self, messages, tools):
                raise TimeoutError("timeout")

        agent = Agent(CrashingBackend([]), registry)
        agent.run(task, log)

        events = log.replay()
        assert events[-1].event_type == EventType.TASK_FAILED
        assert events[-1].payload["failure_type"] == "timeout"


# ---------------------------------------------------------------------------
# Agent.run — 工具不存在
# ---------------------------------------------------------------------------

class TestUnknownTool:
    def test_unknown_tool_does_not_crash_agent(self, task, log):
        """LLM 调用了不存在的工具，agent 应把错误记入 observation 继续运行。"""
        script = [
            make_tool_call_action("nonexistent_tool"),
            make_finish_action(),
        ]
        backend = MockBackend(script)
        registry = ToolRegistry()   # 空 registry，没有任何工具
        agent = Agent(backend, registry)

        result = agent.run(task, log)
        # agent 不应崩溃，最终能完成
        assert result.status == RunStatus.SUCCESS

    def test_unknown_tool_error_in_observation(self, task, log):
        from agent.task import EventType

        script = [
            make_tool_call_action("ghost"),
            make_finish_action(),
        ]
        backend = MockBackend(script)
        registry = ToolRegistry()
        agent = Agent(backend, registry)
        agent.run(task, log)

        events = log.replay()
        obs_events = [e for e in events if e.event_type == EventType.OBSERVATION]
        assert len(obs_events) >= 1
        obs = obs_events[0].payload["observation"]
        assert obs["status"] == "error"

    def test_unknown_tool_then_give_up_is_tool_failure(self, task, log):
        script = [
            make_tool_call_action("ghost"),
            make_give_up_action(),
        ]
        backend = MockBackend(script)
        registry = ToolRegistry()
        agent = Agent(backend, registry)

        result = agent.run(task, log)

        assert result.status == RunStatus.GAVE_UP
        assert result.failure_type == "tool_failure"


class TestFailureInference:
    def test_patch_conflict_then_give_up_is_patch_conflict(self, tmp_path):
        task = Task(
            task_id="patchfail1",
            description="Apply conflicting patch",
            repo_path=str(tmp_path),
            max_steps=5,
        )
        path = tmp_path / "target.py"
        path.write_text("x = 1\n", encoding="utf-8")
        log = EventLog.create(task, log_dir=str(tmp_path / "logs"))

        registry = ToolRegistry()
        registry.register(ApplyPatchTool())
        script = [
            make_tool_call_action(
                "apply_patch",
                {
                    "patch_type": "replace_file",
                    "path": str(path),
                    "content": "x = 2\n",
                    "expected_content": "stale\n",
                },
            ),
            make_give_up_action("Patch failed"),
        ]
        backend = MockBackend(script)
        agent = Agent(backend, registry)

        result = agent.run(task, log)

        assert result.status == RunStatus.GAVE_UP
        assert result.failure_type == "patch_conflict"
