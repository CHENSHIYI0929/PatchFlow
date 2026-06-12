"""
tests/test_day6.py

Day 6 测试：config/schema.py、entry/cli.py（Click test runner）。
GitHub Issue 入口依赖网络，只测纯逻辑部分。
"""

from __future__ import annotations

import os
import json
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from config.schema import (
    AppConfig, load_config, merge_cli_overrides, _expand_env, _parse,
)
from entry.cli import cli


# ===========================================================================
# config/schema.py
# ===========================================================================

class TestExpandEnv:
    def test_expands_set_variable(self, monkeypatch):
        monkeypatch.setenv("MY_KEY", "secret123")
        assert _expand_env("${MY_KEY}") == "secret123"

    def test_unset_variable_becomes_empty(self, monkeypatch):
        monkeypatch.delenv("MISSING_VAR", raising=False)
        assert _expand_env("${MISSING_VAR}") == ""

    def test_no_placeholder_unchanged(self):
        assert _expand_env("hello world") == "hello world"

    def test_multiple_placeholders(self, monkeypatch):
        monkeypatch.setenv("A", "foo")
        monkeypatch.setenv("B", "bar")
        assert _expand_env("${A}-${B}") == "foo-bar"


class TestParseConfig:
    def test_defaults_when_empty(self):
        config = _parse({})
        assert config.llm.provider == "anthropic"
        assert config.agent.max_steps == 40
        assert config.tools.shell.timeout == 30
        assert config.context.history_window == 20

    def test_llm_section(self):
        config = _parse({"llm": {"provider": "deepseek", "model": "deepseek-chat"}})
        assert config.llm.provider == "deepseek"
        assert config.llm.model == "deepseek-chat"

    def test_agent_section(self):
        config = _parse({"agent": {"max_steps": 20, "budget_tokens": 40000}})
        assert config.agent.max_steps == 20
        assert config.agent.budget_tokens == 40000

    def test_tools_section(self):
        config = _parse({"tools": {"shell": {"timeout": 60}}})
        assert config.tools.shell.timeout == 60

    def test_context_section(self):
        config = _parse({"context": {"history_window": 10}})
        assert config.context.history_window == 10

    def test_context_memory_and_compression_options(self):
        config = _parse({
            "context": {
                "enable_compression": False,
                "enable_long_memory": False,
                "long_memory_limit": 2,
            }
        })
        assert config.context.enable_compression is False
        assert config.context.enable_long_memory is False
        assert config.context.long_memory_limit == 2

    def test_partial_section_uses_defaults(self):
        config = _parse({"llm": {"provider": "openai"}})
        assert config.llm.provider == "openai"
        assert config.llm.model == "claude-sonnet-4-5"   # default

    def test_base_url_none_becomes_empty(self):
        config = _parse({"llm": {"base_url": None}})
        assert config.llm.base_url == ""


class TestLoadConfig:
    def test_load_from_file(self, tmp_path):
        yaml_content = """
llm:
  provider: deepseek
  model: deepseek-chat
  api_key: sk-test
agent:
  max_steps: 15
"""
        config_file = tmp_path / "test.yaml"
        config_file.write_text(yaml_content)
        config = load_config(config_file)
        assert config.llm.provider == "deepseek"
        assert config.agent.max_steps == 15

    def test_env_var_expanded_in_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_API_KEY", "sk-from-env")
        yaml_content = "llm:\n  api_key: ${TEST_API_KEY}\n"
        config_file = tmp_path / "test.yaml"
        config_file.write_text(yaml_content)
        config = load_config(config_file)
        assert config.llm.api_key == "sk-from-env"

    def test_missing_file_returns_defaults(self, tmp_path):
        config = load_config(tmp_path / "nonexistent.yaml")
        assert isinstance(config, AppConfig)
        assert config.llm.provider == "anthropic"

    def test_none_path_returns_defaults(self, tmp_path, monkeypatch):
        # 确保当前目录没有 default.yaml
        monkeypatch.chdir(tmp_path)
        config = load_config(None)
        assert isinstance(config, AppConfig)


class TestMergeCliOverrides:
    def test_override_model(self):
        config = _parse({})
        config = merge_cli_overrides(config, model="gpt-4o")
        assert config.llm.model == "gpt-4o"

    def test_override_provider(self):
        config = _parse({})
        config = merge_cli_overrides(config, provider="openai")
        assert config.llm.provider == "openai"

    def test_override_max_steps(self):
        config = _parse({})
        config = merge_cli_overrides(config, max_steps=10)
        assert config.agent.max_steps == 10

    def test_none_values_not_applied(self):
        config = _parse({"agent": {"max_steps": 25}})
        config = merge_cli_overrides(config, max_steps=None)
        assert config.agent.max_steps == 25   # 未被覆盖

    def test_override_api_key(self):
        config = _parse({})
        config = merge_cli_overrides(config, api_key="sk-override")
        assert config.llm.api_key == "sk-override"


# ===========================================================================
# entry/cli.py — Click runner 测试
# ===========================================================================

class TestCliHelp:
    def test_root_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "Coding Agent" in result.output

    def test_run_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["run", "--help"])
        assert result.exit_code == 0
        assert "--repo" in result.output
        assert "--task" in result.output
        assert "--model" in result.output

    def test_log_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["log", "--help"])
        assert result.exit_code == 0

    def test_log_show_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["log", "show", "--help"])
        assert result.exit_code == 0

    def test_log_list_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["log", "list", "--help"])
        assert result.exit_code == 0

    def test_benchmark_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["benchmark", "--help"])
        assert result.exit_code == 0

    def test_benchmark_summarize_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["benchmark", "summarize", "--help"])
        assert result.exit_code == 0

    def test_benchmark_compare_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["benchmark", "compare", "--help"])
        assert result.exit_code == 0

    def test_benchmark_run_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["benchmark", "run", "--help"])
        assert result.exit_code == 0


class TestCliRun:
    """使用 MockBackend 测试 run 命令，不消耗真实 API。"""

    def _invoke_run(self, tmp_path, task="fix it", extra_args=None):
        """辅助：用 MockBackend 跑 CLI run 命令。"""
        from agent.task import Action, ActionType
        from llm.base import MockBackend

        script = [Action(ActionType.FINISH, "done", message="Task complete")]
        mock_backend = MockBackend(script)

        runner = CliRunner()
        args = [
            "run",
            "--repo", str(tmp_path),
            "--task", task,
        ]
        if extra_args:
            args.extend(extra_args)

        # patch create_backend_from_config 返回 mock
        with patch("entry.cli.create_backend_from_config", return_value=mock_backend):
            with patch("entry.cli.load_config") as mock_cfg:
                from config.schema import AppConfig, AgentCfg, LLMConfig, ContextConfig
                cfg = AppConfig()
                cfg.agent.log_dir = str(tmp_path / "logs")
                mock_cfg.return_value = cfg
                result = runner.invoke(cli, args, obj={})

        return result

    def test_run_succeeds(self, tmp_path):
        result = self._invoke_run(tmp_path)
        assert result.exit_code == 0, result.output
        assert "SUCCESS" in result.output

    def test_run_shows_model_info(self, tmp_path):
        result = self._invoke_run(tmp_path)
        assert "Model" in result.output or "Provider" in result.output

    def test_run_exports_artifacts(self, tmp_path):
        result = self._invoke_run(tmp_path)
        assert "Artifacts:" in result.output
        artifact_root = tmp_path / "logs" / "artifacts"
        assert artifact_root.exists()
        artifact_dirs = [p for p in artifact_root.iterdir() if p.is_dir()]
        assert artifact_dirs
        artifact_dir = artifact_dirs[0]
        assert (artifact_dir / "metrics.json").exists()
        assert (artifact_dir / "events.json").exists()

    def test_run_missing_task_fails(self, tmp_path):
        runner = CliRunner()
        result = runner.invoke(cli, ["run", "--repo", str(tmp_path)], obj={})
        assert result.exit_code != 0

    def test_run_nonexistent_repo_fails(self, tmp_path):
        runner = CliRunner()
        with patch("entry.cli.load_config") as mock_cfg:
            from config.schema import AppConfig
            mock_cfg.return_value = AppConfig()
            with patch("entry.cli.create_backend_from_config", return_value=MagicMock()):
                result = runner.invoke(cli, [
                    "run",
                    "--repo", str(tmp_path / "no_such_dir"),
                    "--task", "fix it",
                ], obj={})
        assert result.exit_code != 0

    def test_run_from_task_file(self, tmp_path):
        task_file = tmp_path / "task.txt"
        task_file.write_text("Fix the parser bug")
        # task-file 和 --task 不能同时用，这里只覆盖 task-file 路径
        from agent.task import Action, ActionType
        from llm.base import MockBackend
        script = [Action(ActionType.FINISH, "done", message="ok")]
        mock_backend = MockBackend(script)
        runner = CliRunner()
        with patch("entry.cli.create_backend_from_config", return_value=mock_backend):
            with patch("entry.cli.load_config") as mock_cfg:
                from config.schema import AppConfig
                cfg = AppConfig()
                cfg.agent.log_dir = str(tmp_path / "logs")
                mock_cfg.return_value = cfg
                result = runner.invoke(cli, [
                    "run",
                    "--repo", str(tmp_path),
                    "--task-file", str(task_file),
                ], obj={})
        assert result.exit_code == 0

    def test_registry_prefers_apply_patch_before_file_write(self):
        from entry.cli import _build_registry
        registry = _build_registry(None)
        assert registry.tool_names.index("apply_patch") < registry.tool_names.index("file_write")

    def test_registry_includes_revert_patch_and_graph_neighbors(self):
        from entry.cli import _build_registry
        registry = _build_registry(None)
        assert "revert_patch" in registry.tool_names
        assert "graph_neighbors" in registry.tool_names


class TestCliBenchmark:
    def test_benchmark_summarize_outputs_summary(self, tmp_path):
        artifact_root = tmp_path / "logs" / "artifacts"
        run_a = artifact_root / "run-a"
        run_b = artifact_root / "run-b"
        run_a.mkdir(parents=True)
        run_b.mkdir(parents=True)
        (run_a / "metrics.json").write_text(
            '{"task_success": true, "steps_taken": 4, "total_tokens": 100, '
            '"elapsed_seconds": 1.5, "tool_call_count": 3, "retrieval_queries": 1, '
            '"retrieval_match_count": 2, "patch_attempts": 1, "patch_successes": 1, '
            '"verify_task_calls": 2, "targeted_test_calls": 1, "broad_verification_rejections": 0}',
            encoding="utf-8",
        )
        (run_b / "metrics.json").write_text(
            '{"task_success": false, "steps_taken": 6, "total_tokens": 300, '
            '"elapsed_seconds": 2.5, "tool_call_count": 5, "retrieval_queries": 2, '
            '"retrieval_match_count": 4, "patch_attempts": 2, "patch_successes": 1}',
            encoding="utf-8",
        )

        runner = CliRunner()
        result = runner.invoke(cli, ["benchmark", "summarize", "--dir", str(artifact_root)])
        assert result.exit_code == 0, result.output
        assert "Runs" in result.output
        assert "Success rate" in result.output
        assert "50.00%" in result.output

    def test_benchmark_summarize_json_output(self, tmp_path):
        artifact_root = tmp_path / "logs" / "artifacts"
        run_a = artifact_root / "run-a"
        run_a.mkdir(parents=True)
        (run_a / "metrics.json").write_text(
            '{"task_success": true, "steps_taken": 4, "total_tokens": 100, '
            '"elapsed_seconds": 1.5, "tool_call_count": 3, "retrieval_queries": 1, '
            '"retrieval_match_count": 2, "patch_attempts": 1, "patch_successes": 1, '
            '"preflight_verified": true}',
            encoding="utf-8",
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["benchmark", "summarize", "--dir", str(artifact_root), "--json-output"],
        )
        assert result.exit_code == 0, result.output
        assert '"run_count": 1' in result.output
        assert '"preverified_count": 1' in result.output

    def test_benchmark_summarize_backfills_verification_metrics_from_events(self, tmp_path):
        artifact_root = tmp_path / "logs" / "artifacts"
        run_a = artifact_root / "run-a"
        run_a.mkdir(parents=True)
        (run_a / "metrics.json").write_text(
            '{"task_success": true, "steps_taken": 3, "total_tokens": 80, '
            '"elapsed_seconds": 1.2, "tool_call_count": 2, "retrieval_queries": 0, '
            '"retrieval_match_count": 0, "patch_attempts": 1, "patch_successes": 1}',
            encoding="utf-8",
        )
        (run_a / "events.json").write_text(
            '[{"event_type":"action","payload":{"action":{"tool_call":{"name":"verify_task","params":{}}}}},'
            '{"event_type":"action","payload":{"action":{"tool_call":{"name":"test","params":{"path":"test_demo.py::test_x"}}}}},'
            '{"event_type":"observation","payload":{"observation":{"tool_name":"test","metadata":{"verification_scope_rejected":true}}}}]',
            encoding="utf-8",
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["benchmark", "summarize", "--dir", str(artifact_root), "--json-output"],
        )
        assert result.exit_code == 0, result.output
        assert '"verify_task_calls": 1' in result.output
        assert '"targeted_test_calls": 1' in result.output
        assert '"broad_verification_rejections": 1' in result.output

    def test_benchmark_summarize_only_agent_runs_filters_preverified(self, tmp_path):
        artifact_root = tmp_path / "logs" / "artifacts"
        run_a = artifact_root / "run-a"
        run_b = artifact_root / "run-b"
        run_a.mkdir(parents=True)
        run_b.mkdir(parents=True)
        (run_a / "metrics.json").write_text(
            '{"task_success": true, "steps_taken": 0, "total_tokens": 0, '
            '"elapsed_seconds": 0.2, "tool_call_count": 0, "retrieval_queries": 0, '
            '"retrieval_match_count": 0, "patch_attempts": 0, "patch_successes": 0, '
            '"preflight_verified": true}',
            encoding="utf-8",
        )
        (run_b / "metrics.json").write_text(
            '{"task_success": true, "steps_taken": 4, "total_tokens": 100, '
            '"elapsed_seconds": 1.5, "tool_call_count": 3, "retrieval_queries": 1, '
            '"retrieval_match_count": 2, "patch_attempts": 1, "patch_successes": 1}',
            encoding="utf-8",
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["benchmark", "summarize", "--dir", str(artifact_root), "--only-agent-runs", "--json-output"],
        )
        assert result.exit_code == 0, result.output
        assert '"run_count": 1' in result.output
        assert '"preverified_count": 0' in result.output

    def test_benchmark_summarize_writes_markdown_report(self, tmp_path):
        artifact_root = tmp_path / "logs" / "artifacts"
        run_a = artifact_root / "run-a"
        report_path = tmp_path / "summary.md"
        run_a.mkdir(parents=True)
        (run_a / "metrics.json").write_text(
            '{"task_success": true, "steps_taken": 4, "total_tokens": 100, '
            '"elapsed_seconds": 1.5, "tool_call_count": 3, "retrieval_queries": 1, '
            '"retrieval_match_count": 2, "patch_attempts": 1, "patch_successes": 1, '
            '"verify_task_calls": 2, "targeted_test_calls": 1, "broad_verification_rejections": 0}',
            encoding="utf-8",
        )
        (run_a / "result.json").write_text(
            '{"task_id":"run-a","summary":"ok","failure_type":null,"failure_stage":null,"failure_message":null}',
            encoding="utf-8",
        )
        (run_a / "run_manifest.json").write_text(
            '{"repo_source":"/tmp/source","workspace_repo":"/tmp/workspace"}',
            encoding="utf-8",
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["benchmark", "summarize", "--dir", str(artifact_root), "--markdown-out", str(report_path)],
        )
        assert result.exit_code == 0, result.output
        assert report_path.exists()
        text = report_path.read_text(encoding="utf-8")
        assert "# Benchmark Summary" in text
        assert "Source Repo" in text
        assert "Workspace Repo" in text
        assert "Verify task calls" in text
        assert "| Verify task calls | 2 |" in text

    def test_benchmark_ablation_report_includes_verification_metrics(self, tmp_path):
        artifact_root = tmp_path / "logs" / "artifacts"
        run_a = artifact_root / "run-a"
        report_path = tmp_path / "ablation.md"
        run_a.mkdir(parents=True)
        (run_a / "metrics.json").write_text(
            '{"task_success": true, "steps_taken": 3, "total_tokens": 80, '
            '"elapsed_seconds": 1.2, "tool_call_count": 2, "retrieval_queries": 0, '
            '"retrieval_match_count": 0, "patch_attempts": 1, "patch_successes": 1, '
            '"verify_task_calls": 1, "targeted_test_calls": 0, "broad_verification_rejections": 0}',
            encoding="utf-8",
        )
        (run_a / "result.json").write_text(
            '{"task_id":"run-a","summary":"ok","failure_type":null,"failure_stage":null,"failure_message":null}',
            encoding="utf-8",
        )
        (run_a / "run_manifest.json").write_text(
            '{"mechanisms":{"mechanism_profile":"full"}}',
            encoding="utf-8",
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["benchmark", "ablation-report", "--dir", str(artifact_root), "--markdown-out", str(report_path)],
        )
        assert result.exit_code == 0, result.output
        text = report_path.read_text(encoding="utf-8")
        assert "Verify Task" in text
        assert "Broad Rejections" in text
        assert "| full |" in text

    def test_benchmark_compare_outputs_delta(self, tmp_path):
        left_root = tmp_path / "left"
        right_root = tmp_path / "right"
        left_run = left_root / "run-a"
        right_run = right_root / "run-b"
        left_run.mkdir(parents=True)
        right_run.mkdir(parents=True)
        (left_run / "metrics.json").write_text(
            '{"task_success": false, "steps_taken": 6, "total_tokens": 200, '
            '"elapsed_seconds": 3.0, "tool_call_count": 4, "retrieval_queries": 2, '
            '"retrieval_match_count": 5, "patch_attempts": 2, "patch_successes": 1}',
            encoding="utf-8",
        )
        (right_run / "metrics.json").write_text(
            '{"task_success": true, "steps_taken": 4, "total_tokens": 120, '
            '"elapsed_seconds": 2.0, "tool_call_count": 3, "retrieval_queries": 1, '
            '"retrieval_match_count": 2, "patch_attempts": 1, "patch_successes": 1}',
            encoding="utf-8",
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["benchmark", "compare", "--left", str(left_root), "--right", str(right_root)],
        )
        assert result.exit_code == 0, result.output
        assert "Success rate delta" in result.output

    def test_benchmark_compare_json_output(self, tmp_path):
        left_root = tmp_path / "left"
        right_root = tmp_path / "right"
        left_run = left_root / "run-a"
        right_run = right_root / "run-b"
        left_run.mkdir(parents=True)
        right_run.mkdir(parents=True)
        (left_run / "metrics.json").write_text(
            '{"task_success": false, "steps_taken": 6, "total_tokens": 200, '
            '"elapsed_seconds": 3.0, "tool_call_count": 4, "retrieval_queries": 2, '
            '"retrieval_match_count": 5, "patch_attempts": 2, "patch_successes": 1}',
            encoding="utf-8",
        )
        (right_run / "metrics.json").write_text(
            '{"task_success": true, "steps_taken": 4, "total_tokens": 120, '
            '"elapsed_seconds": 2.0, "tool_call_count": 3, "retrieval_queries": 1, '
            '"retrieval_match_count": 2, "patch_attempts": 1, "patch_successes": 1}',
            encoding="utf-8",
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "benchmark", "compare",
                "--left", str(left_root),
                "--right", str(right_root),
                "--json-output",
            ],
        )
        assert result.exit_code == 0, result.output
        assert '"delta"' in result.output

    def test_benchmark_compare_only_agent_runs_filters_preverified(self, tmp_path):
        left_root = tmp_path / "left"
        right_root = tmp_path / "right"
        left_run = left_root / "run-a"
        right_run = right_root / "run-b"
        left_run.mkdir(parents=True)
        right_run.mkdir(parents=True)
        (left_run / "metrics.json").write_text(
            '{"task_success": true, "steps_taken": 0, "total_tokens": 0, '
            '"elapsed_seconds": 0.2, "tool_call_count": 0, "retrieval_queries": 0, '
            '"retrieval_match_count": 0, "patch_attempts": 0, "patch_successes": 0, '
            '"preflight_verified": true}',
            encoding="utf-8",
        )
        (right_run / "metrics.json").write_text(
            '{"task_success": true, "steps_taken": 4, "total_tokens": 120, '
            '"elapsed_seconds": 2.0, "tool_call_count": 3, "retrieval_queries": 1, '
            '"retrieval_match_count": 2, "patch_attempts": 1, "patch_successes": 1}',
            encoding="utf-8",
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "benchmark", "compare",
                "--left", str(left_root),
                "--right", str(right_root),
                "--only-agent-runs",
                "--json-output",
            ],
        )
        assert result.exit_code == 0, result.output
        assert '"preverified_count": 0' in result.output

    def test_benchmark_compare_writes_markdown_report(self, tmp_path):
        left_root = tmp_path / "left"
        right_root = tmp_path / "right"
        left_run = left_root / "run-a"
        right_run = right_root / "run-b"
        report_path = tmp_path / "compare.md"
        left_run.mkdir(parents=True)
        right_run.mkdir(parents=True)
        (left_run / "metrics.json").write_text(
            '{"task_success": false, "steps_taken": 6, "total_tokens": 200, '
            '"elapsed_seconds": 3.0, "tool_call_count": 4, "retrieval_queries": 2, '
            '"retrieval_match_count": 5, "patch_attempts": 2, "patch_successes": 1}',
            encoding="utf-8",
        )
        (left_run / "result.json").write_text(
            '{"task_id":"run-a","failure_type":"verification_failed","failure_stage":"grading","failure_message":"lint failed"}',
            encoding="utf-8",
        )
        (right_run / "metrics.json").write_text(
            '{"task_success": true, "steps_taken": 4, "total_tokens": 120, '
            '"elapsed_seconds": 2.0, "tool_call_count": 3, "retrieval_queries": 1, '
            '"retrieval_match_count": 2, "patch_attempts": 1, "patch_successes": 1}',
            encoding="utf-8",
        )
        (right_run / "result.json").write_text(
            '{"task_id":"run-b","failure_type":"timeout","failure_stage":"grading","failure_message":"timed out"}',
            encoding="utf-8",
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "benchmark", "compare",
                "--left", str(left_root),
                "--right", str(right_root),
                "--markdown-out", str(report_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert report_path.exists()
        text = report_path.read_text(encoding="utf-8")
        assert "# Benchmark Compare" in text
        assert "Current Failure Types" in text
        assert "Failure Message" in text

    def test_benchmark_run_executes_task_files(self, tmp_path):
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        (tasks_dir / "task-a.txt").write_text("Fix task A", encoding="utf-8")

        runner = CliRunner()
        from agent.task import Action, ActionType
        from llm.base import MockBackend
        script = [Action(ActionType.FINISH, "done", message="Task complete")]
        mock_backend = MockBackend(script)

        with patch("entry.cli.create_backend_from_config", return_value=mock_backend):
            with patch("entry.cli.load_config") as mock_cfg:
                from config.schema import AppConfig
                cfg = AppConfig()
                cfg.agent.log_dir = str(tmp_path / "logs")
                mock_cfg.return_value = cfg
                result = runner.invoke(
                    cli,
                    [
                        "benchmark", "run",
                        "--repo", str(tmp_path),
                        "--tasks-dir", str(tasks_dir),
                    ],
                    obj={},
                )

        assert result.exit_code == 0, result.output
        assert "Benchmark batch complete" in result.output
        artifact_root = tmp_path / "logs" / "artifacts"
        assert artifact_root.exists()

    def test_benchmark_run_accepts_workspace_root(self, tmp_path):
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        source_repo = tmp_path / "demo_repo"
        source_repo.mkdir()
        workspace_root = tmp_path / "outside_workspaces"
        (tasks_dir / "task-a.txt").write_text(
            "---\nrepo: demo_repo\n---\nFix task A",
            encoding="utf-8",
        )
        captured_runs = []

        def fake_execute_run(config, repo_path, description, **kwargs):
            captured_runs.append(str(repo_path))
            class _Result:
                status = type("_S", (), {"value": "success"})()
                steps_taken = 1
                total_tokens = 1
                error = None
                def is_success(self):
                    return True
            return _Result(), tmp_path / "logs" / "artifacts" / "fake"

        runner = CliRunner()
        with patch("entry.cli.load_config") as mock_cfg:
            with patch("entry.cli._execute_run", side_effect=fake_execute_run):
                from config.schema import AppConfig
                cfg = AppConfig()
                cfg.agent.log_dir = str(tmp_path / "logs")
                mock_cfg.return_value = cfg
                result = runner.invoke(
                    cli,
                    [
                        "benchmark", "run",
                        "--repo", str(tmp_path),
                        "--tasks-dir", str(tasks_dir),
                        "--workspace-root", str(workspace_root),
                    ],
                    obj={},
                )

        assert result.exit_code == 0, result.output
        assert captured_runs
        assert Path(captured_runs[0]).parent == workspace_root.resolve()
        assert "Workspace:" in result.output

    def test_prepare_clean_workspace_creates_isolated_git_repo(self, tmp_path):
        from agent.benchmark import prepare_clean_workspace

        source = tmp_path / "source"
        source.mkdir()
        (source / "demo.py").write_text("x = 1\n", encoding="utf-8")
        workspace = prepare_clean_workspace(
            source,
            workspace_root=tmp_path / "outside_workspaces",
            task_name="task-a",
        )

        assert (workspace / ".git").exists()
        (workspace / "demo.py").write_text("x = 2\n", encoding="utf-8")
        diff = subprocess.run(
            ["git", "diff", "HEAD"],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert "demo.py" in diff
        assert "source" not in diff

    def test_benchmark_task_front_matter_is_respected(self, tmp_path):
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        scoped_repo = tmp_path / "demo_repo"
        scoped_repo.mkdir()
        (tasks_dir / "task-a.txt").write_text(
            "---\nrepo: demo_repo\ntest_path: test_demo.py\ntarget_files: core.py, helpers.py\nfinish_if_verified: true\nmax_steps: 7\n---\nFix task A",
            encoding="utf-8",
        )

        runner = CliRunner()
        from agent.task import Action, ActionType
        from llm.base import MockBackend
        script = [Action(ActionType.FINISH, "done", message="Task complete")]
        mock_backend = MockBackend(script)

        captured_runs = []

        def fake_execute_run(config, repo_path, description, **kwargs):
            captured_runs.append(
                {
                    "repo_path": str(repo_path),
                    "source_repo_path": kwargs.get("source_repo_path"),
                    "task_file": kwargs.get("task_file"),
                    "manifest": kwargs.get("manifest"),
                    "description": description,
                    "test_cmd": kwargs.get("test_cmd"),
                    "target_files": kwargs.get("target_files"),
                    "finish_if_verified": kwargs.get("finish_if_verified"),
                    "max_steps": config.agent.max_steps,
                }
            )
            class _Result:
                status = type("_S", (), {"value": "success"})()
                steps_taken = 1
                total_tokens = 1
                error = None
                def is_success(self):
                    return True
            return _Result(), tmp_path / "logs" / "artifacts" / "fake"

        with patch("entry.cli.create_backend_from_config", return_value=mock_backend):
            with patch("entry.cli.load_config") as mock_cfg:
                with patch("entry.cli._execute_run", side_effect=fake_execute_run):
                    from config.schema import AppConfig
                    cfg = AppConfig()
                    cfg.agent.log_dir = str(tmp_path / "logs")
                    mock_cfg.return_value = cfg
                    result = runner.invoke(
                        cli,
                        [
                            "benchmark", "run",
                            "--repo", str(tmp_path),
                            "--tasks-dir", str(tasks_dir),
                        ],
                        obj={},
                    )

        assert result.exit_code == 0, result.output
        assert captured_runs == [
            {
                "repo_path": captured_runs[0]["repo_path"],
                "source_repo_path": str(scoped_repo.resolve()),
                "task_file": str((tasks_dir / "task-a.txt").resolve()),
                "manifest": captured_runs[0]["manifest"],
                "description": "Fix task A",
                "test_cmd": "python -m pytest test_demo.py --tb=short --no-header -q",
                "target_files": ["core.py", "helpers.py"],
                "finish_if_verified": True,
                "max_steps": 7,
            }
        ]
        assert Path(captured_runs[0]["repo_path"]).name.startswith("task-a_")
        assert str(tmp_path / "logs") not in captured_runs[0]["repo_path"]
        assert captured_runs[0]["manifest"]["workspace_repo"] == captured_runs[0]["repo_path"]
        assert captured_runs[0]["manifest"]["repo_source"] == str(tmp_path.resolve())
        assert captured_runs[0]["manifest"]["run_started_at"] is None
        assert captured_runs[0]["manifest"]["run_finished_at"] is None
        assert captured_runs[0]["manifest"]["runtime_type"] == "local"
        assert captured_runs[0]["manifest"]["sandbox_enabled"] is False

    def test_benchmark_patch_replay_applies_patch(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        target = repo / "sample.py"
        target.write_text("x = 1\n", encoding="utf-8")

        artifact_dir = tmp_path / "artifact"
        artifact_dir.mkdir()
        (artifact_dir / "patches.json").write_text(
            '[{"tool_name":"apply_patch","patch":{"patch_type":"search_replace","path":"sample.py","search":"x = 1","replace":"x = 2"},"reverse_patch":{"patch_type":"replace_file","path":"sample.py","content":"x = 1\\n"}}]',
            encoding="utf-8",
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "benchmark", "patch-replay",
                "--artifact-dir", str(artifact_dir),
                "--repo", str(repo),
            ],
            obj={},
        )

        assert result.exit_code == 0, result.output
        assert "Patch Replay" in result.output
        assert target.read_text(encoding="utf-8") == "x = 2\n"

        reverse_result = runner.invoke(
            cli,
            [
                "benchmark", "patch-replay",
                "--artifact-dir", str(artifact_dir),
                "--repo", str(repo),
                "--reverse",
            ],
            obj={},
        )
        assert reverse_result.exit_code == 0, reverse_result.output
        assert target.read_text(encoding="utf-8") == "x = 1\n"

    def test_benchmark_run_skips_preverified_task(self, tmp_path):
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        scoped_repo = tmp_path / "demo_repo"
        scoped_repo.mkdir()
        (scoped_repo / "test_demo.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
        (tasks_dir / "task-a.txt").write_text(
            "---\nrepo: demo_repo\ntest_path: test_demo.py\nfinish_if_verified: true\nskip_preverified: true\n---\nFix task A",
            encoding="utf-8",
        )

        runner = CliRunner()
        execute_calls = []

        def fake_execute_run(*args, **kwargs):
            execute_calls.append((args, kwargs))
            raise AssertionError("_execute_run should not be called for preverified tasks")

        with patch("entry.cli.load_config") as mock_cfg:
            with patch("entry.cli._execute_run", side_effect=fake_execute_run):
                from config.schema import AppConfig
                cfg = AppConfig()
                cfg.agent.log_dir = str(tmp_path / "logs")
                mock_cfg.return_value = cfg
                result = runner.invoke(
                    cli,
                    [
                        "benchmark", "run",
                        "--repo", str(tmp_path),
                        "--tasks-dir", str(tasks_dir),
                    ],
                    obj={},
                )

        assert result.exit_code == 0, result.output
        assert "Preflight : target verification already passes" in result.output
        assert execute_calls == []

    def test_benchmark_run_executes_when_preflight_fails(self, tmp_path):
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        scoped_repo = tmp_path / "demo_repo"
        scoped_repo.mkdir()
        (scoped_repo / "test_demo.py").write_text(
            "def test_not_ok():\n    assert False\n",
            encoding="utf-8",
        )
        (tasks_dir / "task-a.txt").write_text(
            "---\nrepo: demo_repo\ntest_path: test_demo.py\nfinish_if_verified: true\nskip_preverified: true\n---\nFix task A",
            encoding="utf-8",
        )

        runner = CliRunner()
        execute_calls = []

        def fake_execute_run(config, repo_path, description, **kwargs):
            execute_calls.append(
                {
                    "repo_path": str(repo_path),
                    "source_repo_path": kwargs.get("source_repo_path"),
                    "description": description,
                    "test_cmd": kwargs.get("test_cmd"),
                }
            )
            class _Result:
                status = type("_S", (), {"value": "success"})()
                steps_taken = 1
                total_tokens = 1
                error = None
                def is_success(self):
                    return True
            return _Result(), tmp_path / "logs" / "artifacts" / "fake"

        with patch("entry.cli.load_config") as mock_cfg:
            with patch("entry.cli._execute_run", side_effect=fake_execute_run):
                from config.schema import AppConfig
                cfg = AppConfig()
                cfg.agent.log_dir = str(tmp_path / "logs")
                mock_cfg.return_value = cfg
                result = runner.invoke(
                    cli,
                    [
                        "benchmark", "run",
                        "--repo", str(tmp_path),
                        "--tasks-dir", str(tasks_dir),
                    ],
                    obj={},
                )

        assert result.exit_code == 0, result.output
        assert "Preflight : target verification already passes" not in result.output
        assert execute_calls == [
            {
                "repo_path": execute_calls[0]["repo_path"],
                "source_repo_path": str(scoped_repo.resolve()),
                "description": "Fix task A",
                "test_cmd": "python -m pytest test_demo.py --tb=short --no-header -q",
            }
        ]
        assert Path(execute_calls[0]["repo_path"]).name.startswith("task-a_")
        assert str(tmp_path / "logs") not in execute_calls[0]["repo_path"]

    def test_benchmark_reset_fixtures_restores_baseline(self, tmp_path):
        fixtures_root = tmp_path / "benchmark_fixtures"
        target = fixtures_root / "flash_demo"
        baseline = fixtures_root / "_baseline" / "flash_demo"
        target.mkdir(parents=True)
        baseline.mkdir(parents=True)
        (target / "buggy_math.py").write_text("changed = True\n", encoding="utf-8")
        (baseline / "buggy_math.py").write_text("original = True\n", encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["benchmark", "reset-fixtures", "--repo", str(tmp_path)],
        )
        assert result.exit_code == 0, result.output
        assert "Restored benchmark fixtures" in result.output
        assert (target / "buggy_math.py").read_text(encoding="utf-8") == "original = True\n"

    def test_benchmark_run_respects_task_glob_and_limit(self, tmp_path):
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        (tasks_dir / "a.txt").write_text("Fix task A", encoding="utf-8")
        (tasks_dir / "b.txt").write_text("Fix task B", encoding="utf-8")
        (tasks_dir / "skip.md").write_text("Ignore me", encoding="utf-8")

        runner = CliRunner()
        from agent.task import Action, ActionType
        from llm.base import MockBackend
        script = [Action(ActionType.FINISH, "done", message="Task complete")]
        mock_backend = MockBackend(script)

        with patch("entry.cli.create_backend_from_config", return_value=mock_backend):
            with patch("entry.cli.load_config") as mock_cfg:
                from config.schema import AppConfig
                cfg = AppConfig()
                cfg.agent.log_dir = str(tmp_path / "logs")
                mock_cfg.return_value = cfg
                result = runner.invoke(
                    cli,
                    [
                        "benchmark", "run",
                        "--repo", str(tmp_path),
                        "--tasks-dir", str(tasks_dir),
                        "--task-glob", "*.txt",
                        "--limit", "1",
                    ],
                    obj={},
                )

        assert result.exit_code == 0, result.output
        assert "Tasks    : 1" in result.output

    def test_benchmark_run_exports_workspace_error_artifact(self, tmp_path):
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        scoped_repo = tmp_path / "demo_repo"
        scoped_repo.mkdir()
        (tasks_dir / "task-a.txt").write_text(
            "---\nrepo: demo_repo\n---\nFix task A",
            encoding="utf-8",
        )

        runner = CliRunner()
        with patch("entry.cli.load_config") as mock_cfg:
            with patch("agent.benchmark.prepare_clean_workspace", side_effect=OSError("disk full")):
                from config.schema import AppConfig
                cfg = AppConfig()
                cfg.agent.log_dir = str(tmp_path / "logs")
                mock_cfg.return_value = cfg
                result = runner.invoke(
                    cli,
                    [
                        "benchmark", "run",
                        "--repo", str(tmp_path),
                        "--tasks-dir", str(tasks_dir),
                    ],
                    obj={},
                )

        assert result.exit_code == 0, result.output
        assert "Workspace" in result.output
        artifact_root = tmp_path / "logs" / "artifacts"
        artifact_dirs = [p for p in artifact_root.iterdir() if p.is_dir()]
        assert artifact_dirs
        payload = (artifact_dirs[0] / "result.json").read_text(encoding="utf-8")
        assert '"failure_type": "workspace_error"' in payload

    def test_benchmark_run_skips_task_after_timeout(self, tmp_path):
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        scoped_repo = tmp_path / "demo_repo"
        scoped_repo.mkdir()
        (tasks_dir / "task-a.txt").write_text(
            "---\nrepo: demo_repo\n---\nFix task A",
            encoding="utf-8",
        )

        runner = CliRunner()

        def fake_execute_run(*args, **kwargs):
            time.sleep(2.0)
            raise AssertionError("timeout wrapper should interrupt before this point")

        with patch("entry.cli.load_config") as mock_cfg:
            with patch("entry.cli._execute_run", side_effect=fake_execute_run):
                from config.schema import AppConfig
                cfg = AppConfig()
                cfg.agent.log_dir = str(tmp_path / "logs")
                mock_cfg.return_value = cfg
                result = runner.invoke(
                    cli,
                    [
                        "benchmark", "run",
                        "--repo", str(tmp_path),
                        "--tasks-dir", str(tasks_dir),
                        "--task-timeout-seconds", "1",
                    ],
                    obj={},
                )

        assert result.exit_code == 0, result.output
        artifact_root = tmp_path / "logs" / "artifacts"
        artifact_dirs = [p for p in artifact_root.iterdir() if p.is_dir()]
        assert artifact_dirs
        result_payload = (artifact_dirs[0] / "result.json").read_text(encoding="utf-8")
        manifest_payload = (artifact_dirs[0] / "run_manifest.json").read_text(encoding="utf-8")
        assert "0/1 succeeded" in result.output
        assert '"failure_type": "timeout"' in result_payload
        assert '"failure_stage": "agent_loop"' in result_payload
        assert '"task_timeout_seconds": 1' in manifest_payload


class TestBenchmarkTaskSpecs:
    def test_load_task_spec_plain_text(self, tmp_path):
        from agent.benchmark import load_task_spec

        task_file = tmp_path / "task.txt"
        task_file.write_text("Fix the bug", encoding="utf-8")
        spec = load_task_spec(task_file)

        assert spec.description == "Fix the bug"
        assert spec.repo is None
        assert spec.finish_if_verified is True

    def test_load_task_spec_front_matter(self, tmp_path):
        from agent.benchmark import load_task_spec

        task_file = tmp_path / "task.txt"
        task_file.write_text(
            "---\nrepo: benchmark_fixtures/run_demo\ntest_path: test_report.py\nlint_cmd: python -m pytest test_lint.py -q\npatch_policy_cmd: python check_patch.py\nexclude_paths: logs, README.md\ntarget_files: report.py, scores.py\nfinish_if_verified: false\nmax_steps: 9\n---\nFix the report bug",
            encoding="utf-8",
        )
        spec = load_task_spec(task_file)

        assert spec.repo == "benchmark_fixtures/run_demo"
        assert spec.test_path == "test_report.py"
        assert spec.lint_cmd == "python -m pytest test_lint.py -q"
        assert spec.patch_policy_cmd == "python check_patch.py"
        assert spec.exclude_paths == ["logs", "README.md"]
        assert spec.target_files == ["report.py", "scores.py"]
        assert spec.finish_if_verified is False
        assert spec.max_steps == 9
        assert spec.description == "Fix the report bug"

    def test_load_task_spec_v2_metadata(self, tmp_path):
        from agent.benchmark import load_task_spec

        task_file = tmp_path / "task.txt"
        task_file.write_text(
            "---\ncategory: bugfix\ndifficulty: medium\nexpected_failure_type: verification_failed\n---\nFix it",
            encoding="utf-8",
        )
        spec = load_task_spec(task_file)

        assert spec.category == "bugfix"
        assert spec.difficulty == "medium"
        assert spec.expected_failure_type == "verification_failed"

    def test_build_run_manifest_includes_runtime_metadata(self, tmp_path):
        from agent.benchmark import build_run_manifest
        from config.schema import AppConfig

        manifest = build_run_manifest(
            task_id="run-1",
            task_file=None,
            task_repo=tmp_path,
            source_repo=tmp_path,
            workspace_repo=tmp_path,
            config=AppConfig(),
            grader=None,
            sandbox=False,
        )

        assert manifest["run_started_at"] is None
        assert manifest["run_finished_at"] is None
        assert manifest["runtime_type"] == "local"
        assert manifest["sandbox_enabled"] is False
        assert manifest["platform"]
        assert manifest["python_version"]

    def test_command_grader_timeout_is_classified(self, tmp_path):
        from agent.grader import CommandGrader

        repo = tmp_path / "repo"
        repo.mkdir()
        grader = CommandGrader("test", "python -c \"import time; time.sleep(1.5)\"")
        result = grader.run(repo, timeout=1)

        assert result.success is False
        assert result.failure_type == "timeout"
        assert "timed out" in result.message.lower()

    def test_preverified_benchmark_run_records_memory(self, tmp_path):
        from agent.benchmark import BenchmarkTaskSpec, try_preverify_task

        repo = tmp_path / "repo"
        repo.mkdir()
        task_file = tmp_path / "task.txt"
        task_file.write_text("Already fixed", encoding="utf-8")
        log_dir = tmp_path / "logs"
        spec = BenchmarkTaskSpec(
            path=task_file,
            description="Already fixed",
            test_cmd="python -c \"print('ok')\"",
        )

        preverified = try_preverify_task(spec, repo, log_dir=str(log_dir))

        assert preverified is not None
        memory_path = log_dir / "memory" / "run_memory.jsonl"
        rows = [json.loads(line) for line in memory_path.read_text(encoding="utf-8").splitlines()]
        assert rows[-1]["status"] == "success"
        assert rows[-1]["test_cmd"] == spec.test_cmd

    def test_failed_benchmark_artifact_records_memory(self, tmp_path):
        from agent.benchmark import BenchmarkTaskSpec, export_failed_benchmark_artifact
        from agent.failure import FAILURE_STAGE_PREVERIFY, FAILURE_TYPE_WORKSPACE_ERROR, failure

        repo = tmp_path / "repo"
        repo.mkdir()
        task_file = tmp_path / "task.txt"
        task_file.write_text("Fix setup", encoding="utf-8")
        log_dir = tmp_path / "logs"
        spec = BenchmarkTaskSpec(path=task_file, description="Fix setup")

        export_failed_benchmark_artifact(
            spec=spec,
            repo_path=repo,
            log_dir=str(log_dir),
            manifest=None,
            failure=failure(
                "Workspace setup failed",
                failure_type=FAILURE_TYPE_WORKSPACE_ERROR,
                failure_stage=FAILURE_STAGE_PREVERIFY,
            ),
        )

        memory_path = log_dir / "memory" / "run_memory.jsonl"
        rows = [json.loads(line) for line in memory_path.read_text(encoding="utf-8").splitlines()]
        assert rows[-1]["status"] == "failed"
        assert rows[-1]["failure_type"] == "workspace_error"


class TestCliLog:
    def test_log_show(self, tmp_path):
        """写一个真实 event log，用 CLI 读取。"""
        from agent.event_log import EventLog
        from agent.task import Task, Action, ActionType
        from llm.base import MockBackend
        from agent.core import Agent
        from tools.base import NoopTool, ToolRegistry

        task = Task(
            task_id="logtest1",
            description="test task",
            repo_path=str(tmp_path),
        )
        registry = ToolRegistry().register(NoopTool("shell"))
        script = [Action(ActionType.FINISH, "done", message="ok")]
        backend = MockBackend(script)
        agent = Agent(backend, registry)

        with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
            agent.run(task, log)
            log_path = log.path

        runner = CliRunner()
        result = runner.invoke(cli, ["log", "show", str(log_path)], obj={})
        assert result.exit_code == 0
        assert "Total events" in result.output

    def test_log_list(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "abc_20240101.jsonl").write_text('{"event_type":"task_start"}\n')

        runner = CliRunner()
        result = runner.invoke(cli, ["log", "list", "--dir", str(log_dir)], obj={})
        assert result.exit_code == 0
        assert "abc_20240101.jsonl" in result.output

    def test_log_list_empty(self, tmp_path):
        log_dir = tmp_path / "empty_logs"
        log_dir.mkdir()
        runner = CliRunner()
        result = runner.invoke(cli, ["log", "list", "--dir", str(log_dir)], obj={})
        assert result.exit_code == 0
        assert "No log files" in result.output


# ===========================================================================
# entry/github_issue.py — 纯逻辑测试（不发真实网络请求）
# ===========================================================================

class TestGitHubIssueLogic:
    def test_fetch_issue_no_token_raises(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        from entry.github_issue import fetch_issue
        with pytest.raises(ValueError, match="GITHUB_TOKEN"):
            fetch_issue("owner/repo", 1)

    def test_fetch_issue_mocked(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
        from entry.github_issue import fetch_issue

        mock_issue = MagicMock()
        mock_issue.title = "Fix the parser"
        mock_issue.body = "The parser crashes on empty input"
        mock_issue.html_url = "https://github.com/owner/repo/issues/1"

        mock_repo = MagicMock()
        mock_repo.get_issue.return_value = mock_issue

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        with patch("entry.github_issue._get_github_client", return_value=mock_gh):
            title, body, url = fetch_issue("owner/repo", 1)

        assert title == "Fix the parser"
        assert "empty input" in body
        assert "issues/1" in url

    def test_create_pr_mocked(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
        from entry.github_issue import create_pull_request

        mock_pr = MagicMock()
        mock_pr.html_url = "https://github.com/owner/repo/pull/99"

        mock_repo = MagicMock()
        mock_repo.get_branch.return_value = MagicMock()
        mock_repo.create_pull.return_value = mock_pr

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        with patch("entry.github_issue._get_github_client", return_value=mock_gh):
            url = create_pull_request("owner/repo", "agent/fix-1", "Fix it", "body")

        assert "pull/99" in url
