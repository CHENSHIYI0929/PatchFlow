"""
entry/cli.py

命令行入口。

用法：
    # 直接传任务描述
    python -m entry.cli run --repo /path/to/repo --task "Fix the failing test"

    # 从文件读任务描述
    python -m entry.cli run --repo . --task-file task.txt

    # 覆盖模型
    python -m entry.cli run --repo . --task "fix it" --model deepseek-chat

    # 查看 event log 统计
    python -m entry.cli log show logs/abc123_20240101_120000.jsonl

安装为命令行工具后（pyproject.toml 里配置了 scripts）：
    agent run --repo . --task "fix it"
"""

from __future__ import annotations

import logging
import json
import multiprocessing
import queue
import sys
import tempfile
import time
import uuid
from pathlib import Path

import click

# 把项目根加入 path（直接跑脚本时需要）
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config.schema import load_config, merge_cli_overrides  # noqa: E402
from llm.router import create_backend_from_config           # noqa: E402
from agent.failure import (  # noqa: E402
    FAILURE_STAGE_AGENT_LOOP,
    FAILURE_STAGE_PREVERIFY,
    FAILURE_TYPE_TIMEOUT,
    FAILURE_TYPE_WORKSPACE_ERROR,
    failure,
)


# ---------------------------------------------------------------------------
# 辅助：彩色输出
# ---------------------------------------------------------------------------

def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text

def green(t: str) -> str:  return _c(t, "32")
def yellow(t: str) -> str: return _c(t, "33")
def red(t: str) -> str:    return _c(t, "31")
def cyan(t: str) -> str:   return _c(t, "36")
def bold(t: str) -> str:   return _c(t, "1")
def dim(t: str) -> str:    return _c(t, "2")
def magenta(t: str) -> str: return _c(t, "35")


class TaskTimeoutError(TimeoutError):
    """Raised when a benchmark task exceeds its wall-clock timeout."""


def _benchmark_run_worker(result_queue, payload: dict) -> None:
    """Run a single benchmark task in a child process."""
    try:
        result, artifact_dir = _execute_run(**payload)
        result_queue.put({
            "ok": True,
            "result": result,
            "artifact_dir": artifact_dir,
        })
    except BaseException as exc:  # pragma: no cover - exercised through parent process
        result_queue.put({
            "ok": False,
            "error": str(exc),
            "error_type": type(exc).__name__,
        })


def _execute_run_with_task_timeout(seconds: int | None, payload: dict):
    """Execute one benchmark task, using a worker process when a hard timeout is configured."""
    if not seconds or seconds <= 0:
        return _execute_run(**payload)

    try:
        ctx = multiprocessing.get_context("fork")
    except ValueError:  # pragma: no cover - platform fallback
        ctx = multiprocessing.get_context()
    result_queue = ctx.Queue(maxsize=1)
    process = ctx.Process(target=_benchmark_run_worker, args=(result_queue, payload))
    process.start()
    process.join(seconds)

    if process.is_alive():
        process.terminate()
        process.join(5)
        if process.is_alive() and hasattr(process, "kill"):
            process.kill()
            process.join(1)
        raise TaskTimeoutError(f"Timed out after {seconds}s")

    try:
        message = result_queue.get_nowait()
    except queue.Empty as exc:
        raise RuntimeError(f"Benchmark worker exited without a result (exitcode={process.exitcode})") from exc
    finally:
        result_queue.close()
        result_queue.join_thread()

    if message.get("ok"):
        return message["result"], message["artifact_dir"]
    raise RuntimeError(f"{message.get('error_type')}: {message.get('error')}")


# ---------------------------------------------------------------------------
# 构建 agent 各组件
# ---------------------------------------------------------------------------

def _build_registry(cfg, confirm_callback=None, runtime=None):
    """根据配置组装工具注册表。"""
    from tools.base import ToolRegistry
    from tools.file_tool import ApplyPatchTool, FileReadTool, FileViewTool, FileWriteTool, RevertPatchTool
    from tools.git_tool import GitAddTool, GitCommitTool, GitDiffTool, GitStatusTool
    from tools.search_tool import FindFilesTool, FindSymbolTool, GraphNeighborsTool, SearchTextTool
    from tools.shell_tool import ShellTool
    from tools.test_tool import PytestTool

    return (
        ToolRegistry()
        .register(ShellTool(confirm_callback=confirm_callback, runtime=runtime))
        .register(FileReadTool())
        .register(FileViewTool())
        .register(ApplyPatchTool())
        .register(RevertPatchTool())
        .register(FileWriteTool())
        .register(SearchTextTool())
        .register(FindFilesTool())
        .register(FindSymbolTool())
        .register(GraphNeighborsTool())
        .register(PytestTool(runtime=runtime))
        .register(GitStatusTool(runtime=runtime))
        .register(GitDiffTool(runtime=runtime))
        .register(GitAddTool(runtime=runtime))
        .register(GitCommitTool(runtime=runtime))
    )


def _print_step(event) -> None:
    """实时打印单条 event。"""
    from agent.task import EventType
    etype = event.event_type
    payload = event.payload

    if etype == EventType.TASK_START:
        task = payload["task"]
        click.echo(bold(f"\n{'─'*60}"))
        click.echo(bold(f"  Task : {task['description'][:80]}"))
        click.echo(bold(f"  Repo : {task['repo_path']}"))
        click.echo(bold(f"{'─'*60}\n"))

    elif etype == EventType.ACTION:
        step = payload["step"]
        action = payload["action"]
        thought = action.get("thought", "")[:160]
        atype = action.get("action_type", "")
        tc = action.get("tool_call")
        click.echo(cyan(f"[Step {step}] {atype}"))
        if thought:
            click.echo(dim(f"  ↳ {thought}"))
        if tc:
            params_str = str(tc["params"])[:100]
            click.echo(f"  Tool: {tc['name']}  params: {params_str}")

    elif etype == EventType.OBSERVATION:
        obs = payload["observation"]
        status = obs.get("status", "")
        tool = obs.get("tool_name", "")
        output = obs.get("output", "")
        if status == "success":
            click.echo(green(f"  ✓ [{tool}]"))
        else:
            click.echo(red(f"  ✗ [{tool}] {obs.get('error', '')}"))
        # 打印前 5 行输出
        for line in output.splitlines()[:5]:
            click.echo(dim(f"    {line}"))
        if len(output.splitlines()) > 5:
            click.echo(dim(f"    ... ({len(output.splitlines())-5} more lines)"))
        click.echo()

    elif etype == EventType.REFLECTION:
        click.echo(yellow(f"\n  ⟳ Reflection: {payload.get('reason', '')}\n"))

    elif etype == EventType.TASK_COMPLETE:
        click.echo(green(bold(f"\n✓ COMPLETE: {payload.get('summary', '')}\n")))

    elif etype == EventType.TASK_FAILED:
        click.echo(red(bold(f"\n✗ FAILED: {payload.get('reason', '')}\n")))


def _resolve_task_description(task: str | None, task_file: str | None) -> str | None:
    if task_file:
        return Path(task_file).read_text(encoding="utf-8").strip()
    if task:
        return task
    return None


def _execute_run(
    config,
    repo_path: Path,
    description: str,
    *,
    task_file: str | None = None,
    source_repo_path: str | None = None,
    test_cmd: str | None = None,
    exclude_paths: list[str] | None = None,
    target_files: list[str] | None = None,
    finish_if_verified: bool = False,
    grader=None,
    manifest: dict | None = None,
    stream: bool,
    confirm: bool,
    sandbox: bool,
    verbose: bool,
    show_banner: bool = True,
    run_mode: str = "auto",
    disable_failure_analyzer: bool = False,
    disable_hybrid_retrieval: bool = False,
    disable_edit_plan: bool = False,
    disable_self_review: bool = False,
    disable_long_memory: bool = False,
    disable_compression: bool = False,
):
    """Shared run execution for `run` and benchmark batch mode."""
    if show_banner:
        click.echo(bold(f"\n🤖 Coding Agent"))
        click.echo(f"  Provider : {config.llm.provider}")
        click.echo(f"  Model    : {config.llm.model}")
        click.echo(f"  Repo     : {repo_path}")
        click.echo(f"  Max steps: {config.agent.max_steps}\n")

    try:
        backend = create_backend_from_config({
            "provider": config.llm.provider,
            "model":    config.llm.model,
            "api_key":  config.llm.api_key or None,
            "base_url": config.llm.base_url or None,
            "max_tokens": config.llm.max_tokens,
        })
    except ValueError as e:
        click.echo(red(f"Error: {e}"), err=True)
        raise click.Abort() from e

    from tools.shell_tool import terminal_confirm
    from tools.runtime import create_runtime
    confirm_cb = terminal_confirm if confirm else None
    runtime = create_runtime(sandbox=sandbox, repo_path=str(repo_path)) if sandbox else None
    if sandbox and show_banner:
        click.echo(dim(f"  Sandbox: Docker ({runtime.name})"))
    registry = _build_registry(config, confirm_callback=confirm_cb, runtime=runtime)

    from agent.core import Agent, AgentConfig
    from agent.event_log import EventLog
    from agent.task import Task
    try:
        from context.token_budget import is_tiktoken_available
    except ImportError:
        is_tiktoken_available = lambda: False

    def _stream_cb(text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    def _thought_cb(text: str) -> None:
        sys.stdout.write(dim(text))
        sys.stdout.flush()

    agent_config = AgentConfig(
        max_steps=config.agent.max_steps,
        budget_tokens=config.agent.budget_tokens,
        history_max_messages=config.context.history_window * 2,
        enable_context_compression=config.context.enable_compression and not disable_compression,
        enable_long_memory=config.context.enable_long_memory and not disable_long_memory,
        long_memory_limit=config.context.long_memory_limit,
        log_dir=config.agent.log_dir,
        run_mode=run_mode,
        failure_analyzer_enabled=not disable_failure_analyzer,
        hybrid_retrieval_enabled=not disable_hybrid_retrieval,
        edit_plan_enabled=not disable_edit_plan,
        self_review_on_finish=not disable_self_review,
        patch_self_review_enabled=not disable_self_review,
        stream=stream,
        stream_callback=_stream_cb if stream else None,
        thought_callback=_thought_cb if stream else None,
        confirm_dangerous=confirm,
        confirm_callback=confirm_cb,
    )
    agent = Agent(backend, registry, agent_config)
    mechanisms = {
        "run_mode": run_mode,
        "failure_analyzer": not disable_failure_analyzer,
        "hybrid_retrieval": not disable_hybrid_retrieval,
        "edit_plan": not disable_edit_plan,
        "self_review": not disable_self_review,
        "long_memory": config.context.enable_long_memory and not disable_long_memory,
        "compression": config.context.enable_compression and not disable_compression,
    }
    if manifest is not None:
        manifest.setdefault("mechanisms", {}).update(mechanisms)

    task_obj = Task(
        description=description,
        repo_path=str(repo_path),
        source_repo_path=source_repo_path or str(repo_path),
        task_file=task_file,
        test_cmd=test_cmd,
        exclude_paths=exclude_paths or [],
        target_files=target_files or [],
        finish_if_verified=finish_if_verified,
        max_steps=config.agent.max_steps,
        budget_tokens=config.agent.budget_tokens,
    )

    if verbose and show_banner:
        click.echo(dim(
            f"  tiktoken: {'yes' if is_tiktoken_available() else 'no (char estimate)'}\n"
        ))

    t0 = time.time()
    try:
        with EventLog.create(task_obj, log_dir=config.agent.log_dir) as log:
            click.echo(dim(f"  Log: {log.path}\n"))
            result = agent.run(task_obj, log)
            if grader is not None and result.is_success():
                grader_result = grader.run(repo_path, runtime=runtime, timeout=120)
                from agent.task import Observation, ObservationStatus, RunStatus
                grade_observation = Observation(
                    status=ObservationStatus.SUCCESS if grader_result.success else ObservationStatus.ERROR,
                    output=grader_result.output or grader_result.message,
                    tool_name="final_grader",
                    error=None if grader_result.success else grader_result.message,
                    metadata={
                        "grader": grader.describe(),
                        "checks": grader_result.checks,
                        "command": grader_result.command,
                    },
                )
                log.log_observation(step=result.steps_taken + 1, observation=grade_observation)
                if not grader_result.success:
                    failure_reason = f"Final grading failed: {grader_result.message}"
                    log.log_task_failed(
                        steps=result.steps_taken,
                        reason=failure_reason,
                        failure_type=grader_result.failure_type or "verification_failed",
                        failure_stage=grader_result.stage,
                        failure_message=grader_result.output or grader_result.message,
                    )
                    result = type(result)(
                        task_id=result.task_id,
                        status=RunStatus.FAILED,
                        summary=failure_reason,
                        steps_taken=result.steps_taken,
                        total_tokens=result.total_tokens,
                        patch=result.patch,
                        error=grader_result.message,
                        failure_type=grader_result.failure_type or "verification_failed",
                        failure_stage=grader_result.stage,
                        failure_message=grader_result.output or grader_result.message,
                    )
            for event in log.replay():
                _print_step(event)

        elapsed = time.time() - t0
        from agent.artifacts import export_run_artifacts
        from agent.memory import append_run_memory
        artifact_dir = export_run_artifacts(log, result, elapsed, manifest=manifest)
        append_run_memory(config.agent.log_dir, task_obj, result)
    finally:
        if runtime is not None:
            runtime.cleanup()

    click.echo(bold("─" * 60))
    status_str = green("SUCCESS") if result.is_success() else red(result.status.value.upper())
    click.echo(f"Status  : {status_str}")
    click.echo(f"Steps   : {result.steps_taken}")
    click.echo(f"Tokens  : {result.total_tokens:,}")
    click.echo(f"Time    : {elapsed:.1f}s")
    click.echo(f"Artifacts: {artifact_dir}")
    if result.error:
        click.echo(red(f"Error   : {result.error}"))
    if result.failure_type:
        click.echo(red(f"Failure : {result.failure_type} @ {result.failure_stage}"))
    click.echo(bold("─" * 60) + "\n")

    return result, artifact_dir


# ---------------------------------------------------------------------------
# CLI 主命令组
# ---------------------------------------------------------------------------

@click.group()
@click.option(
    "--config", "-c",
    default=None,
    help="Path to config YAML file (default: config/default.yaml)",
)
@click.pass_context
def cli(ctx: click.Context, config: str | None) -> None:
    """Coding Agent — autonomous code editing and bug fixing."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config


# ---------------------------------------------------------------------------
# run 子命令
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--repo", "-r", default=".", show_default=True, help="Path to the target repository (default: current directory)")
@click.option("--task", "-t", default=None, help="Task description (natural language)")
@click.option("--task-file", "-f", default=None, help="Read task description from file")
@click.option("--model", "-m", default=None, help="Override LLM model name")
@click.option("--provider", "-p", default=None, help="Override LLM provider")
@click.option("--max-steps", default=None, type=int, help="Override max steps")
@click.option("--stream", "-s", is_flag=True, default=True, help="Enable streaming output (default: on)")
@click.option("--run-mode", type=click.Choice(["safe", "review", "auto"]), default="auto", show_default=True, help="Execution mode")
@click.option("--disable-failure-analyzer", is_flag=True, default=False, help="Disable structured failure analysis")
@click.option("--disable-hybrid-retrieval", is_flag=True, default=False, help="Disable hybrid retrieval hints")
@click.option("--disable-edit-plan", is_flag=True, default=False, help="Disable edit plan validation")
@click.option("--disable-self-review", is_flag=True, default=False, help="Disable patch and finish self-review")
@click.option("--disable-long-memory", is_flag=True, default=False, help="Disable long memory retrieval")
@click.option("--disable-compression", is_flag=True, default=False, help="Disable context compression")
@click.option("--confirm", is_flag=True, default=False, help="Ask confirmation before running dangerous shell commands")
@click.option("--sandbox", is_flag=True, default=False, help="Run commands in Docker sandbox (requires Docker)")
@click.option("--verbose", "-v", is_flag=True, help="Show debug logs")
@click.pass_context
def run(
    ctx: click.Context,
    repo: str,
    task: str | None,
    task_file: str | None,
    model: str | None,
    provider: str | None,
    max_steps: int | None,
    stream: bool,
    run_mode: str,
    disable_failure_analyzer: bool,
    disable_hybrid_retrieval: bool,
    disable_edit_plan: bool,
    disable_self_review: bool,
    disable_long_memory: bool,
    disable_compression: bool,
    confirm: bool,
    sandbox: bool,
    verbose: bool,
) -> None:
    """Run the coding agent on a repository."""
    # 配置日志
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    # 加载配置
    config = load_config(ctx.obj.get("config_path"))
    config = merge_cli_overrides(
        config, provider=provider, model=model, max_steps=max_steps
    )

    # 解析任务描述
    description = _resolve_task_description(task, task_file)
    if not description:
        click.echo(red("Error: provide --task or --task-file"), err=True)
        sys.exit(1)

    repo_path = Path(repo).resolve()
    if not repo_path.exists():
        click.echo(red(f"Error: repo path does not exist: {repo_path}"), err=True)
        sys.exit(1)

    from agent.benchmark import build_run_manifest
    manifest = build_run_manifest(
        task_id="adhoc-run",
        task_file=task_file,
        task_repo=repo_path,
        source_repo=repo_path,
        workspace_repo=repo_path,
        config=config,
        grader=None,
        sandbox=sandbox,
    )

    result, _artifact_dir = _execute_run(
        config,
        repo_path,
        description,
        task_file=task_file,
        source_repo_path=str(repo_path),
        manifest=manifest,
        test_cmd=None,
        exclude_paths=None,
        target_files=None,
        finish_if_verified=False,
        stream=stream,
        confirm=confirm,
        sandbox=sandbox,
        verbose=verbose,
        show_banner=True,
        run_mode=run_mode,
        disable_failure_analyzer=disable_failure_analyzer,
        disable_hybrid_retrieval=disable_hybrid_retrieval,
        disable_edit_plan=disable_edit_plan,
        disable_self_review=disable_self_review,
        disable_long_memory=disable_long_memory,
        disable_compression=disable_compression,
    )
    sys.exit(0 if result.is_success() else 1)



# ---------------------------------------------------------------------------
# chat 子命令 — 交互对话模式
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--repo", "-r", default=".", show_default=True, help="Path to the target repository (default: current directory)")
@click.option("--model", "-m", default=None, help="Override LLM model name")
@click.option("--provider", "-p", default=None, help="Override LLM provider")
@click.option("--max-steps", default=None, type=int, help="Max steps per round")
@click.option("--sandbox", is_flag=True, default=False, help="Run commands in Docker sandbox (requires Docker)")
@click.option("--verbose", "-v", is_flag=True, help="Show debug logs")
@click.pass_context
def chat(
    ctx: click.Context,
    repo: str,
    model: str | None,
    provider: str | None,
    max_steps: int | None,
    sandbox: bool,
    verbose: bool,
) -> None:
    """Interactive chat mode — continuous conversation with the agent."""
    import logging
    from entry.chat import ChatSession

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    config = load_config(ctx.obj.get("config_path"))
    config = merge_cli_overrides(config, provider=provider, model=model, max_steps=max_steps)

    repo_path = Path(repo).resolve()
    if not repo_path.exists():
        click.echo(red(f"Error: repo path does not exist: {repo_path}"), err=True)
        sys.exit(1)

    try:
        backend = create_backend_from_config({
            "provider":   config.llm.provider,
            "model":      config.llm.model,
            "api_key":    config.llm.api_key or None,
            "base_url":   config.llm.base_url or None,
            "max_tokens": config.llm.max_tokens,
        })
    except ValueError as e:
        click.echo(red(f"Error: {e}"), err=True)
        sys.exit(1)

    registry = _build_registry(config)
    from tools.shell_tool import terminal_confirm
    from tools.runtime import create_runtime
    runtime = create_runtime(sandbox=sandbox, repo_path=str(repo_path)) if sandbox else None
    if sandbox:
        click.echo(dim(f"  Sandbox: Docker ({runtime.name})"))
    session = ChatSession(
        backend=backend,
        registry=registry,
        config=config,
        repo_path=str(repo_path),
        log_dir=config.agent.log_dir,
        confirm_callback=terminal_confirm,   # chat 模式默认开启确认
    )

    # 欢迎信息
    click.echo(bold(f"\n🤖 Coding Agent — Chat Mode"))
    click.echo(f"  Provider : {config.llm.provider}")
    click.echo(f"  Model    : {config.llm.model}")
    click.echo(f"  Repo     : {repo_path}")
    click.echo(dim(f"  Type your task. Commands: /exit /stats /clear /help\n"))

    # 启用行编辑：退格、方向键、Ctrl+A/E、历史记录（↑↓）
    try:
        import readline as _rl
        import sys as _sys
        # 检测后端：libedit（某些 Linux/macOS）还是 GNU readline
        _is_libedit = "libedit" in getattr(_rl, "__doc__", "") or (
            hasattr(_rl, "parse_and_bind") and _sys.platform == "darwin"
        )
        # 更可靠的检测：尝试 libedit 特有的绑定语法
        try:
            _rl.parse_and_bind("bind -e")   # libedit 启用 Emacs 模式
            _is_libedit = True
        except Exception:
            _is_libedit = False

        if _is_libedit:
            _rl.parse_and_bind("bind -e")           # Emacs 模式：Ctrl+A/E/K 等
            _rl.parse_and_bind("bind ^I rl_complete")  # Tab 补全
        else:
            _rl.parse_and_bind("set editing-mode emacs")  # GNU readline Emacs 模式
            _rl.parse_and_bind("tab: complete")

        _rl.set_history_length(500)   # 历史记录最多 500 条
    except ImportError:
        pass  # Windows 没有 readline，降级为普通 input

    # 主 REPL 循环
    while True:
        try:
            # 清理当前行（流式输出后 readline 不知道屏幕上有残留字符）
            # \r 回到行首，\033[2K 清除整行，然后显示提示符
            sys.stdout.write("\r\033[2K")
            sys.stdout.flush()
            user_input = input(magenta("you") + " > ").strip()
        except EOFError:
            click.echo()
            break
        except KeyboardInterrupt:
            click.echo()
            break

        if not user_input:
            continue

        # 内置命令
        if user_input.startswith("/"):
            cmd = user_input.lower()
            if cmd in ("/exit", "/quit", "/q"):
                break
            elif cmd == "/stats":
                session.print_stats()
            elif cmd == "/clear":
                session._shared_history.clear_except_first()
                click.echo(dim("  History cleared (kept initial context)."))
            elif cmd == "/help":
                click.echo(dim(
                    "  Commands:\n"
                    "    /exit   — quit\n"
                    "    /stats  — show session statistics\n"
                    "    /clear  — clear conversation history\n"
                    "    /help   — show this help\n"
                    "  Anything else is sent to the agent."
                ))
            else:
                click.echo(dim(f"  Unknown command: {user_input}. Type /help for help."))
            continue

        # 运行一轮 agent
        click.echo(dim(f"\n  Agent working..."))
        try:
            session.run_round(user_input)
        except KeyboardInterrupt:
            click.echo(yellow("\n  Interrupted. Type /exit to quit or continue with a new task."))
        except Exception as e:
            click.echo(red(f"\n  Error: {e}"))
            if verbose:
                import traceback
                traceback.print_exc()

    session.print_stats()
    click.echo(dim("  Bye!\n"))


# ---------------------------------------------------------------------------
# log 子命令组
# ---------------------------------------------------------------------------

@cli.group()
def log() -> None:
    """Inspect event logs."""


@log.command("show")
@click.argument("log_file")
def log_show(log_file: str) -> None:
    """Show a summary of an event log file."""
    from agent.event_log import EventLog, summarize_run

    path = Path(log_file)
    if not path.exists():
        click.echo(red(f"File not found: {path}"), err=True)
        sys.exit(1)

    with EventLog.open_existing(path) as elog:
        events = elog.replay()
        stats = summarize_run(elog)

    click.echo(bold(f"\nEvent Log: {path.name}"))
    click.echo(f"  Total events : {stats['total_events']}")
    click.echo(f"  Actions      : {stats['actions']}")
    click.echo(f"  Reflections  : {stats['reflections']}")
    click.echo(f"  Tool calls   : {stats['tool_calls']}")
    click.echo(f"  Final status : {stats['final_status']}\n")

    click.echo(bold("Events:"))
    for event in events:
        ts = event.timestamp[11:19]   # HH:MM:SS
        etype = event.event_type.value
        detail = ""
        if event.event_type.value == "action":
            tc = event.payload.get("action", {}).get("tool_call")
            detail = f"  tool={tc['name']}" if tc else ""
        elif event.event_type.value == "observation":
            obs = event.payload.get("observation", {})
            detail = f"  status={obs.get('status')}"
        click.echo(f"  {ts}  {etype:<16}{detail}")


@log.command("list")
@click.option("--dir", "log_dir", default="./logs", help="Log directory")
def log_list(log_dir: str) -> None:
    """List all event log files."""
    log_path = Path(log_dir)
    if not log_path.exists():
        click.echo(f"Log directory not found: {log_path}")
        return

    files = sorted(log_path.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        click.echo("No log files found.")
        return

    click.echo(bold(f"\nLog files in {log_path}:\n"))
    for f in files:
        size_kb = f.stat().st_size / 1024
        click.echo(f"  {f.name}  ({size_kb:.1f} KB)")
    click.echo()


# ---------------------------------------------------------------------------
# benchmark 子命令组
# ---------------------------------------------------------------------------

@cli.group()
def benchmark() -> None:
    """Summarize exported run artifacts."""


@benchmark.command("summarize")
@click.option("--dir", "artifact_dir", default="./logs/artifacts", help="Artifact root directory")
@click.option("--only-agent-runs", is_flag=True, default=False, help="Exclude preflight-verified runs and summarize only real agent executions")
@click.option("--json-output", is_flag=True, default=False, help="Print the summary as JSON")
@click.option("--markdown-out", default=None, help="Write the summary to a Markdown file")
def benchmark_summarize(
    artifact_dir: str,
    only_agent_runs: bool,
    json_output: bool,
    markdown_out: str | None,
) -> None:
    """Aggregate metrics.json files under an artifact directory."""
    from agent.benchmark import summarize_artifacts
    import json

    root = Path(artifact_dir)
    if not root.exists():
        click.echo(red(f"Artifact directory not found: {root}"), err=True)
        sys.exit(1)

    summary = summarize_artifacts(root, include_preverified=not only_agent_runs)
    if markdown_out:
        report_path = Path(markdown_out)
        report_path.write_text(_render_benchmark_summary_markdown(summary), encoding="utf-8")
    if json_output:
        click.echo(json.dumps(summary, ensure_ascii=False, indent=2))
        return

    click.echo(bold(f"\nBenchmark Summary: {root}"))
    click.echo(f"  Runs               : {summary['run_count']}")
    click.echo(f"  Successes          : {summary['success_count']}")
    click.echo(f"  Success rate       : {summary['success_rate']:.2%}")
    click.echo(f"  Preverified runs   : {summary['preverified_count']}")
    click.echo(f"  Scope              : {'agent-only' if only_agent_runs else 'all runs'}")
    click.echo(f"  Avg steps          : {summary['avg_steps']}")
    click.echo(f"  Avg tokens         : {summary['avg_tokens']}")
    click.echo(f"  Avg time           : {summary['avg_elapsed_seconds']}s")
    click.echo(f"  Avg tool calls     : {summary['avg_tool_calls']}")
    click.echo(f"  Avg retrievals     : {summary['avg_retrieval_queries']}")
    click.echo(f"  Avg retrieval hits : {summary['avg_retrieval_match_count']}")
    click.echo(f"  Avg graph queries  : {summary['avg_graph_queries']}")
    click.echo(f"  Avg patch attempts : {summary['avg_patch_attempts']}")
    click.echo(f"  Patch success rate : {summary['patch_success_rate']:.2%}\n")
    click.echo(f"  Patch conflicts    : {summary['patch_conflicts']}")
    click.echo(f"  Patch reverts      : {summary['patch_reverts']}\n")
    click.echo(f"  First-pass success : {summary['first_pass_success_rate']:.2%}")
    click.echo(f"  Finish verify fails: {summary['finish_verification_failures']}")
    click.echo(f"  Self-review fails  : {summary['self_review_failures']}")
    click.echo(f"  Recovery prompts   : {summary['taxonomy_recovery_prompts']}")
    click.echo(f"  Auto symbol probes : {summary['auto_symbol_probes']}\n")
    click.echo(f"  Long memory hits   : {summary['long_memory_hits']}")
    click.echo(f"  Context compressions: {summary['context_compressions']}\n")
    click.echo(f"  Failure analyses   : {summary['failure_analyses']}")
    click.echo(f"  Edit plans         : {summary['edit_plans']}")
    click.echo(f"  Patch review fails : {summary['patch_review_failures']}\n")
    if markdown_out:
        click.echo(f"  Markdown report    : {markdown_out}\n")


@benchmark.command("compare")
@click.option("--left", "left_dir", required=True, help="Left artifact root directory")
@click.option("--right", "right_dir", required=True, help="Right artifact root directory")
@click.option("--only-agent-runs", is_flag=True, default=False, help="Exclude preflight-verified runs on both sides")
@click.option("--json-output", is_flag=True, default=False, help="Print the comparison as JSON")
@click.option("--markdown-out", default=None, help="Write the comparison summary to a Markdown file")
def benchmark_compare(
    left_dir: str,
    right_dir: str,
    only_agent_runs: bool,
    json_output: bool,
    markdown_out: str | None,
) -> None:
    """Compare two artifact roots."""
    from agent.benchmark import compare_artifact_roots
    import json

    left_root = Path(left_dir)
    right_root = Path(right_dir)
    if not left_root.exists():
        click.echo(red(f"Artifact directory not found: {left_root}"), err=True)
        sys.exit(1)
    if not right_root.exists():
        click.echo(red(f"Artifact directory not found: {right_root}"), err=True)
        sys.exit(1)

    comparison = compare_artifact_roots(
        left_root,
        right_root,
        include_preverified=not only_agent_runs,
    )
    if markdown_out:
        report_path = Path(markdown_out)
        report_path.write_text(_render_benchmark_compare_markdown(comparison), encoding="utf-8")
    if json_output:
        click.echo(json.dumps(comparison, ensure_ascii=False, indent=2))
        return

    click.echo(bold(f"\nBenchmark Compare"))
    click.echo(f"  Left  : {left_root}")
    click.echo(f"  Right : {right_root}\n")
    click.echo(f"  Scope : {'agent-only' if only_agent_runs else 'all runs'}\n")
    click.echo(f"  Success rate delta       : {comparison['delta']['success_rate']:+.2%}")
    click.echo(f"  Preverified delta        : {comparison['delta']['preverified_count']:+.0f}")
    click.echo(f"  Avg steps delta          : {comparison['delta']['avg_steps']:+.3f}")
    click.echo(f"  Avg tokens delta         : {comparison['delta']['avg_tokens']:+.3f}")
    click.echo(f"  Avg time delta           : {comparison['delta']['avg_elapsed_seconds']:+.3f}s")
    click.echo(f"  Avg tool calls delta     : {comparison['delta']['avg_tool_calls']:+.3f}")
    click.echo(f"  Avg retrievals delta     : {comparison['delta']['avg_retrieval_queries']:+.3f}")
    click.echo(f"  Avg retrieval hits delta : {comparison['delta']['avg_retrieval_match_count']:+.3f}")
    click.echo(f"  Avg graph queries delta  : {comparison['delta']['avg_graph_queries']:+.3f}")
    click.echo(f"  Avg patch attempts delta : {comparison['delta']['avg_patch_attempts']:+.3f}")
    click.echo(f"  Patch success delta      : {comparison['delta']['patch_success_rate']:+.2%}\n")
    click.echo(f"  Patch conflict delta     : {comparison['delta']['patch_conflicts']:+.0f}")
    click.echo(f"  Patch revert delta       : {comparison['delta']['patch_reverts']:+.0f}\n")
    if markdown_out:
        click.echo(f"  Markdown report          : {markdown_out}\n")


@benchmark.command("ablation-report")
@click.option("--dir", "artifact_dir", default="./logs/artifacts", help="Artifact root directory")
@click.option("--only-agent-runs", is_flag=True, default=False, help="Exclude preflight-verified runs")
@click.option("--markdown-out", default="ablation_report.md", show_default=True, help="Write ablation report to Markdown")
@click.option("--json-output", is_flag=True, default=False, help="Print grouped summary as JSON")
def benchmark_ablation_report(
    artifact_dir: str,
    only_agent_runs: bool,
    markdown_out: str,
    json_output: bool,
) -> None:
    """Summarize benchmark results grouped by mechanism profile."""
    from agent.benchmark import summarize_by_mechanism

    root = Path(artifact_dir)
    if not root.exists():
        click.echo(red(f"Artifact directory not found: {root}"), err=True)
        sys.exit(1)
    grouped = summarize_by_mechanism(root, include_preverified=not only_agent_runs)
    if json_output:
        click.echo(json.dumps(grouped, ensure_ascii=False, indent=2))
        return
    out = Path(markdown_out)
    out.write_text(_render_ablation_markdown(grouped), encoding="utf-8")
    click.echo(green(f"Ablation report written: {out}"))


@benchmark.command("run")
@click.option("--repo", "-r", default=".", show_default=True, help="Target repository for all task files")
@click.option("--tasks-dir", required=True, help="Directory containing task .txt files")
@click.option("--task-glob", default="*.txt", show_default=True, help="Glob used to select task files")
@click.option("--limit", default=None, type=int, help="Only run the first N matching task files")
@click.option("--task-timeout-seconds", default=None, type=int, help="Skip a task if it runs longer than this many wall-clock seconds")
@click.option("--workspace-root", default=None, help="Directory used for per-task clean benchmark workspaces")
@click.option("--model", "-m", default=None, help="Override LLM model name")
@click.option("--provider", "-p", default=None, help="Override LLM provider")
@click.option("--max-steps", default=None, type=int, help="Override max steps")
@click.option("--skip-preverified/--no-skip-preverified", default=True, show_default=True, help="Skip the LLM run when the task's target verification already passes")
@click.option("--mechanism-profile", type=click.Choice(["baseline", "partial", "full"]), default="full", show_default=True, help="Mechanism profile for ablation runs")
@click.option("--disable-failure-analyzer", is_flag=True, default=False, help="Disable structured failure analysis")
@click.option("--disable-hybrid-retrieval", is_flag=True, default=False, help="Disable hybrid retrieval hints")
@click.option("--disable-edit-plan", is_flag=True, default=False, help="Disable edit plan validation")
@click.option("--disable-self-review", is_flag=True, default=False, help="Disable patch and finish self-review")
@click.option("--disable-long-memory", is_flag=True, default=False, help="Disable long memory retrieval")
@click.option("--disable-compression", is_flag=True, default=False, help="Disable context compression")
@click.option("--stream", "-s", is_flag=True, default=False, help="Enable streaming output")
@click.option("--confirm", is_flag=True, default=False, help="Ask confirmation before dangerous shell commands")
@click.option("--sandbox", is_flag=True, default=False, help="Run commands in Docker sandbox (requires Docker)")
@click.option("--verbose", "-v", is_flag=True, help="Show debug logs")
@click.pass_context
def benchmark_run(
    ctx: click.Context,
    repo: str,
    tasks_dir: str,
    task_glob: str,
    limit: int | None,
    task_timeout_seconds: int | None,
    workspace_root: str | None,
    model: str | None,
    provider: str | None,
    max_steps: int | None,
    skip_preverified: bool,
    mechanism_profile: str,
    disable_failure_analyzer: bool,
    disable_hybrid_retrieval: bool,
    disable_edit_plan: bool,
    disable_self_review: bool,
    disable_long_memory: bool,
    disable_compression: bool,
    stream: bool,
    confirm: bool,
    sandbox: bool,
    verbose: bool,
) -> None:
    """Batch-run a directory of task files and export artifacts for each run."""
    from agent.benchmark import (
        build_grader_for_spec,
        build_run_manifest,
        default_test_cmd_for_spec,
        export_failed_benchmark_artifact,
        load_task_spec,
        prepare_clean_workspace,
        resolve_task_repo,
        try_preverify_task,
    )
    from tools.runtime import create_runtime

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    config = load_config(ctx.obj.get("config_path"))
    config = merge_cli_overrides(config, provider=provider, model=model, max_steps=max_steps)
    if mechanism_profile == "baseline":
        disable_failure_analyzer = True
        disable_hybrid_retrieval = True
        disable_edit_plan = True
        disable_self_review = True
    elif mechanism_profile == "partial":
        disable_edit_plan = True
        disable_self_review = True
    mechanisms = {
        "run_mode": "benchmark",
        "mechanism_profile": mechanism_profile,
        "failure_analyzer": not disable_failure_analyzer,
        "hybrid_retrieval": not disable_hybrid_retrieval,
        "edit_plan": not disable_edit_plan,
        "self_review": not disable_self_review,
        "long_memory": config.context.enable_long_memory and not disable_long_memory,
        "compression": config.context.enable_compression and not disable_compression,
    }

    repo_path = Path(repo).resolve()
    if not repo_path.exists():
        click.echo(red(f"Error: repo path does not exist: {repo_path}"), err=True)
        sys.exit(1)

    tasks_root = Path(tasks_dir).resolve()
    if not tasks_root.exists():
        click.echo(red(f"Error: tasks directory does not exist: {tasks_root}"), err=True)
        sys.exit(1)

    task_files = sorted(tasks_root.glob(task_glob))
    if limit is not None:
        task_files = task_files[:limit]
    if not task_files:
        click.echo(red(f"Error: no task files found in {tasks_root} matching {task_glob!r}"), err=True)
        sys.exit(1)

    success_count = 0
    workspace_root_path = (
        Path(workspace_root).expanduser().resolve()
        if workspace_root
        else Path(tempfile.gettempdir()).resolve() / "patchflow-workspaces" / repo_path.name
    )
    click.echo(bold(f"\n🏁 Benchmark Run"))
    click.echo(f"  Provider : {config.llm.provider}")
    click.echo(f"  Model    : {config.llm.model}")
    click.echo(f"  Repo     : {repo_path}")
    click.echo(f"  Tasks    : {len(task_files)}")
    click.echo(f"  Source   : {tasks_root}")
    click.echo(f"  Workspace: {workspace_root_path}\n")
    for index, task_file in enumerate(task_files, start=1):
        spec = load_task_spec(task_file)
        source_task_repo = resolve_task_repo(repo_path, spec)
        if not source_task_repo.exists():
            click.echo(red(f"Error: task repo path does not exist for {task_file.name}: {source_task_repo}"), err=True)
            sys.exit(1)

        run_config = config
        if spec.max_steps is not None and spec.max_steps != config.agent.max_steps:
            run_config = merge_cli_overrides(config, max_steps=spec.max_steps)
        grader = build_grader_for_spec(spec)
        task_id = f"{task_file.stem}-{uuid.uuid4().hex[:8]}"
        task_repo = source_task_repo
        try:
            task_repo = prepare_clean_workspace(
                source_task_repo,
                workspace_root=workspace_root_path,
                task_name=task_file.stem,
            )
        except Exception as exc:
            failure_info = failure(
                f"Workspace setup failed: {exc}",
                failure_type=FAILURE_TYPE_WORKSPACE_ERROR,
                failure_stage=FAILURE_STAGE_PREVERIFY,
            )
            manifest = build_run_manifest(
                task_id=task_id,
                task_file=str(task_file),
                task_repo=source_task_repo,
                source_repo=repo_path,
                workspace_repo=task_repo,
                config=run_config,
                grader=grader,
                sandbox=sandbox,
            )
            manifest["mechanisms"] = mechanisms
            if task_timeout_seconds is not None:
                manifest["agent_config"]["task_timeout_seconds"] = task_timeout_seconds
            manifest["task_metadata"] = {
                "category": spec.category,
                "difficulty": spec.difficulty,
                "expected_failure_type": spec.expected_failure_type,
            }
            _result, artifact_dir = export_failed_benchmark_artifact(
                spec=spec,
                repo_path=task_repo,
                log_dir=run_config.agent.log_dir,
                manifest=manifest,
                failure=failure_info,
            )
            click.echo(bold(f"[{index}/{len(task_files)}] {task_file.name}"))
            click.echo(red(f"  Workspace  : {exc}"))
            click.echo(f"  Artifacts  : {artifact_dir}\n")
            continue

        manifest = build_run_manifest(
            task_id=task_id,
            task_file=str(task_file),
            task_repo=source_task_repo,
            source_repo=repo_path,
            workspace_repo=task_repo,
            config=run_config,
            grader=grader,
            sandbox=sandbox,
        )
        manifest["mechanisms"] = mechanisms
        if task_timeout_seconds is not None:
            manifest["agent_config"]["task_timeout_seconds"] = task_timeout_seconds
        manifest["task_metadata"] = {
            "category": spec.category,
            "difficulty": spec.difficulty,
            "expected_failure_type": spec.expected_failure_type,
        }

        click.echo(bold(f"[{index}/{len(task_files)}] {task_file.name}"))
        click.echo(dim(f"  Source repo    : {source_task_repo}"))
        click.echo(dim(f"  Workspace repo : {task_repo}"))
        if spec.test_path or spec.test_cmd:
            click.echo(dim(f"  Verify    : {spec.test_cmd or spec.test_path}"))
        if spec.lint_cmd:
            click.echo(dim(f"  Lint      : {spec.lint_cmd}"))
        if spec.patch_policy_cmd:
            click.echo(dim(f"  Patch     : {spec.patch_policy_cmd}"))

        preverified = None
        preverify_runtime = None
        try:
            preverify_runtime = create_runtime(sandbox=sandbox, repo_path=str(task_repo)) if sandbox else None
            effective_spec = spec
            if not skip_preverified:
                effective_spec.skip_preverified = False
            preverified = try_preverify_task(
                effective_spec,
                task_repo,
                log_dir=run_config.agent.log_dir,
                manifest=manifest,
                runtime=preverify_runtime,
            )
        except Exception as exc:
            failure_info = failure(
                f"Preverify failed: {exc}",
                failure_type=FAILURE_TYPE_WORKSPACE_ERROR,
                failure_stage=FAILURE_STAGE_PREVERIFY,
            )
            _result, artifact_dir = export_failed_benchmark_artifact(
                spec=spec,
                repo_path=task_repo,
                log_dir=run_config.agent.log_dir,
                manifest=manifest,
                failure=failure_info,
            )
            click.echo(red(f"  Preverify  : {exc}"))
            click.echo(f"  Artifacts  : {artifact_dir}\n")
            if preverify_runtime is not None:
                preverify_runtime.cleanup()
            continue
        if preverify_runtime is not None:
            preverify_runtime.cleanup()
        if preverified is not None:
            result, artifact_dir = preverified
            click.echo(yellow("  Preflight : target verification already passes; skipping agent run"))
            click.echo(f"  Artifacts : {artifact_dir}\n")
            if result.is_success():
                success_count += 1
            continue

        try:
            result, _artifact_dir = _execute_run_with_task_timeout(
                task_timeout_seconds,
                {
                    "config": run_config,
                    "repo_path": task_repo,
                    "description": spec.description,
                    "task_file": str(task_file),
                    "source_repo_path": str(source_task_repo),
                    "manifest": manifest,
                    "test_cmd": default_test_cmd_for_spec(spec),
                    "exclude_paths": spec.exclude_paths,
                    "target_files": spec.target_files,
                    "finish_if_verified": spec.finish_if_verified,
                    "grader": grader,
                    "stream": stream,
                    "confirm": confirm,
                    "sandbox": sandbox,
                    "verbose": verbose,
                    "show_banner": False,
                    "run_mode": "benchmark",
                    "disable_failure_analyzer": disable_failure_analyzer,
                    "disable_hybrid_retrieval": disable_hybrid_retrieval,
                    "disable_edit_plan": disable_edit_plan,
                    "disable_self_review": disable_self_review,
                    "disable_long_memory": disable_long_memory,
                    "disable_compression": disable_compression,
                },
            )
        except TaskTimeoutError as exc:
            failure_info = failure(
                str(exc),
                failure_type=FAILURE_TYPE_TIMEOUT,
                failure_stage=FAILURE_STAGE_AGENT_LOOP,
            )
            result, artifact_dir = export_failed_benchmark_artifact(
                spec=spec,
                repo_path=task_repo,
                log_dir=run_config.agent.log_dir,
                manifest=manifest,
                failure=failure_info,
            )
            click.echo(red(f"  Timeout    : {exc}"))
            click.echo(f"  Artifacts  : {artifact_dir}\n")
        except Exception as exc:
            failure_info = failure(
                f"Run failed before completion: {exc}",
                failure_type=FAILURE_TYPE_WORKSPACE_ERROR,
                failure_stage=FAILURE_STAGE_PREVERIFY,
            )
            result, artifact_dir = export_failed_benchmark_artifact(
                spec=spec,
                repo_path=task_repo,
                log_dir=run_config.agent.log_dir,
                manifest=manifest,
                failure=failure_info,
            )
            click.echo(red(f"  Run error  : {exc}"))
            click.echo(f"  Artifacts  : {artifact_dir}\n")
        if result.is_success():
            success_count += 1

    click.echo(bold(f"Benchmark batch complete: {success_count}/{len(task_files)} succeeded"))


@benchmark.command("reset-fixtures")
@click.option("--repo", "-r", default=".", show_default=True, help="Repository root that contains benchmark_fixtures")
def benchmark_reset_fixtures(repo: str) -> None:
    """Restore benchmark_fixtures/ from the checked-in baseline snapshot."""
    from agent.benchmark import reset_benchmark_fixtures

    repo_root = Path(repo).resolve()
    if not repo_root.exists():
        click.echo(red(f"Error: repo path does not exist: {repo_root}"), err=True)
        sys.exit(1)

    try:
        fixtures_root = reset_benchmark_fixtures(repo_root)
    except FileNotFoundError as exc:
        click.echo(red(f"Error: {exc}"), err=True)
        sys.exit(1)

    click.echo(green(f"Restored benchmark fixtures from baseline: {fixtures_root}"))


@benchmark.command("patch-replay")
@click.option("--artifact-dir", required=True, help="Artifact directory containing patches.json")
@click.option("--repo", "-r", required=True, help="Repository root to replay the patch into")
@click.option("--patch-index", default=-1, type=int, show_default=True, help="Which structured patch entry to replay (-1 means last)")
@click.option("--reverse", is_flag=True, default=False, help="Replay the reverse_patch instead of the forward patch")
@click.option("--json-output", is_flag=True, default=False, help="Print the replay result as JSON")
def benchmark_patch_replay(
    artifact_dir: str,
    repo: str,
    patch_index: int,
    reverse: bool,
    json_output: bool,
) -> None:
    """Replay a structured patch from patches.json onto a repo."""
    from agent.benchmark import replay_patch_from_artifact
    import json

    try:
        result = replay_patch_from_artifact(
            artifact_dir,
            repo,
            patch_index=patch_index,
            reverse=reverse,
        )
    except Exception as exc:
        click.echo(red(f"Error: {exc}"), err=True)
        sys.exit(1)

    if json_output:
        click.echo(json.dumps(result, ensure_ascii=False, indent=2))
        return

    click.echo(bold("\nPatch Replay"))
    click.echo(f"  Artifact : {result['artifact_dir']}")
    click.echo(f"  Repo     : {result['repo_root']}")
    click.echo(f"  Tool     : {result['selected_tool']}")
    click.echo(f"  Reverse  : {result['reverse']}")
    click.echo(f"  Status   : {green('success') if result['success'] else red('error')}")
    if result["error"]:
        click.echo(red(f"  Error    : {result['error']}"))
    if result["output"]:
        click.echo(dim(f"\n{result['output']}"))


@cli.group()
def patch() -> None:
    """Inspect and rollback patch artifacts."""


@patch.command("latest")
@click.option("--artifact-dir", required=True, help="Artifact directory containing patches.json")
def patch_latest(artifact_dir: str) -> None:
    """Show the latest patch metadata for a run artifact."""
    artifact_path = Path(artifact_dir)
    patches_path = artifact_path / "patches.json"
    if not patches_path.exists():
        click.echo(red(f"Error: patches.json not found in {artifact_path}"), err=True)
        sys.exit(1)
    patches = json.loads(patches_path.read_text(encoding="utf-8"))
    patch_items = [item for item in patches if item.get("tool_name") in {"apply_patch", "file_write"}]
    if not patch_items:
        click.echo(yellow("No apply_patch/file_write entries found."))
        return
    click.echo(json.dumps(patch_items[-1], ensure_ascii=False, indent=2))


@patch.command("rollback")
@click.option("--artifact-dir", required=True, help="Artifact directory containing patches.json")
@click.option("--repo", default=".", show_default=True, help="Repository path where rollback should be applied")
def patch_rollback(artifact_dir: str, repo: str) -> None:
    """Rollback the latest apply_patch using reverse_patch metadata."""
    from tools.file_tool import ApplyPatchTool

    artifact_path = Path(artifact_dir)
    patches_path = artifact_path / "patches.json"
    if not patches_path.exists():
        click.echo(red(f"Error: patches.json not found in {artifact_path}"), err=True)
        sys.exit(1)
    patches = json.loads(patches_path.read_text(encoding="utf-8"))
    patch_items = [
        item for item in patches
        if item.get("tool_name") == "apply_patch" and item.get("reverse_patch")
    ]
    if not patch_items:
        click.echo(red("Error: no rollback-capable patch found."), err=True)
        sys.exit(1)
    reverse_patch = dict(patch_items[-1]["reverse_patch"])
    reverse_path = Path(str(reverse_patch.get("path", "")))
    if not reverse_path.is_absolute():
        reverse_patch["path"] = str(Path(repo).resolve() / reverse_path)
    result = ApplyPatchTool().execute(reverse_patch)
    event = {
        "event_type": "rollback",
        "artifact_dir": str(artifact_path),
        "repo": str(Path(repo).resolve()),
        "success": result.success,
        "output": result.output,
        "error": result.error,
        "reverse_patch": reverse_patch,
    }
    rollback_path = artifact_path / "rollback_events.jsonl"
    with rollback_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    if result.success:
        click.echo(green(result.output))
        click.echo(dim(f"Rollback event: {rollback_path}"))
        return
    click.echo(red(f"Rollback failed: {result.error}"), err=True)
    sys.exit(1)


def _render_benchmark_compare_markdown(comparison: dict) -> str:
    left = comparison["left"]
    right = comparison["right"]
    delta = comparison["delta"]
    right_runs = right.get("runs", [])
    failure_types = right.get("failure_type_distribution", {})
    failure_stages = right.get("failure_stage_distribution", {})
    return "\n".join(
        [
            "# Benchmark Compare",
            "",
            "## Overview",
            "",
            f"- Left: `{left['artifact_root']}`",
            f"- Right: `{right['artifact_root']}`",
            f"- Scope: `{'agent-only' if not left.get('include_preverified', True) else 'all-runs'}`",
            f"- Tasks: {right['run_count']}",
            "",
            "## Baseline Comparison",
            "",
            "| Metric | Left | Right | Delta |",
            "| --- | ---: | ---: | ---: |",
            f"| Runs | {left['run_count']} | {right['run_count']} | {delta['run_count']:+.4f} |",
            f"| Success rate | {left['success_rate']:.2%} | {right['success_rate']:.2%} | {delta['success_rate']:+.2%} |",
            f"| Preverified runs | {left['preverified_count']} | {right['preverified_count']} | {delta['preverified_count']:+.0f} |",
            f"| Avg steps | {left['avg_steps']} | {right['avg_steps']} | {delta['avg_steps']:+.4f} |",
            f"| Avg tokens | {left['avg_tokens']} | {right['avg_tokens']} | {delta['avg_tokens']:+.4f} |",
            f"| Avg time (s) | {left['avg_elapsed_seconds']} | {right['avg_elapsed_seconds']} | {delta['avg_elapsed_seconds']:+.4f} |",
            f"| Avg tool calls | {left['avg_tool_calls']} | {right['avg_tool_calls']} | {delta['avg_tool_calls']:+.4f} |",
            f"| Avg retrievals | {left['avg_retrieval_queries']} | {right['avg_retrieval_queries']} | {delta['avg_retrieval_queries']:+.4f} |",
            f"| Avg retrieval hits | {left['avg_retrieval_match_count']} | {right['avg_retrieval_match_count']} | {delta['avg_retrieval_match_count']:+.4f} |",
            f"| Avg graph queries | {left['avg_graph_queries']} | {right['avg_graph_queries']} | {delta['avg_graph_queries']:+.4f} |",
            f"| Avg patch attempts | {left['avg_patch_attempts']} | {right['avg_patch_attempts']} | {delta['avg_patch_attempts']:+.4f} |",
            f"| Patch success rate | {left['patch_success_rate']:.2%} | {right['patch_success_rate']:.2%} | {delta['patch_success_rate']:+.2%} |",
            f"| Patch conflicts | {left['patch_conflicts']} | {right['patch_conflicts']} | {delta['patch_conflicts']:+.0f} |",
            f"| Patch reverts | {left['patch_reverts']} | {right['patch_reverts']} | {delta['patch_reverts']:+.0f} |",
            "",
            "## Current Failure Types",
            "",
            "| Failure Type | Count |",
            "| --- | ---: |",
            *(f"| {name} | {count} |" for name, count in sorted(failure_types.items())),
            *(["| - | 0 |"] if not failure_types else []),
            "",
            "## Current Failure Stages",
            "",
            "| Failure Stage | Count |",
            "| --- | ---: |",
            *(f"| {name} | {count} |" for name, count in sorted(failure_stages.items())),
            *(["| - | 0 |"] if not failure_stages else []),
            "",
            "## Current Run Results",
            "",
            "| Task | Result | Failure Type | Failure Stage | Failure Message | Patch | Artifact |",
            "| --- | --- | --- | --- | --- | --- | --- |",
            *[
                f"| `{Path(item.get('task_file') or item.get('artifact_dir')).name}` | "
                f"{'success' if item.get('task_success') else 'failed'} | "
                f"{item.get('failure_type') or '-'} | "
                f"{item.get('failure_stage') or '-'} | "
                f"{_markdown_cell(item.get('failure_message'))} | "
                f"{f'`{Path(item['patch_path']).name}`' if item.get('patch_path') else '-'} | "
                f"`{Path(item['artifact_dir']).name}` |"
                for item in right_runs
            ],
            "",
        ]
    )


def _render_benchmark_summary_markdown(summary: dict) -> str:
    failure_types = summary.get("failure_type_distribution", {})
    failure_stages = summary.get("failure_stage_distribution", {})
    return "\n".join(
        [
            "# Benchmark Summary",
            "",
            "## Overview",
            "",
            f"- Artifact root: `{summary['artifact_root']}`",
            f"- Scope: `{'agent-only' if not summary.get('include_preverified', True) else 'all-runs'}`",
            f"- Tasks: {summary['run_count']}",
            f"- Success rate: {summary['success_rate']:.2%}",
            f"- Average steps: {summary['avg_steps']}",
            f"- Average tokens: {summary['avg_tokens']}",
            f"- Average elapsed seconds: {summary['avg_elapsed_seconds']}",
            "",
            "## Aggregates",
            "",
            "| Metric | Value |",
            "| --- | ---: |",
            f"| Successes | {summary['success_count']} |",
            f"| Preverified runs | {summary['preverified_count']} |",
            f"| Avg steps | {summary['avg_steps']} |",
            f"| Avg tokens | {summary['avg_tokens']} |",
            f"| Avg time (s) | {summary['avg_elapsed_seconds']} |",
            f"| Avg tool calls | {summary['avg_tool_calls']} |",
            f"| Avg retrievals | {summary['avg_retrieval_queries']} |",
            f"| Avg retrieval hits | {summary['avg_retrieval_match_count']} |",
            f"| Avg graph queries | {summary['avg_graph_queries']} |",
            f"| Avg patch attempts | {summary['avg_patch_attempts']} |",
            f"| Patch success rate | {summary['patch_success_rate']:.2%} |",
            f"| Patch conflicts | {summary['patch_conflicts']} |",
            f"| Patch reverts | {summary['patch_reverts']} |",
            f"| First-pass success rate | {summary['first_pass_success_rate']:.2%} |",
            f"| Finish verification failures | {summary['finish_verification_failures']} |",
            f"| Self-review failures | {summary['self_review_failures']} |",
            f"| Recovery prompts | {summary['taxonomy_recovery_prompts']} |",
            f"| Auto symbol probes | {summary['auto_symbol_probes']} |",
            f"| Long memory hits | {summary['long_memory_hits']} |",
            f"| Context compressions | {summary['context_compressions']} |",
            f"| Failure analyses | {summary['failure_analyses']} |",
            f"| Edit plans | {summary['edit_plans']} |",
            f"| Patch review failures | {summary['patch_review_failures']} |",
            "",
            "## Failure Type Distribution",
            "",
            "| Failure Type | Count |",
            "| --- | ---: |",
            *(f"| {name} | {count} |" for name, count in sorted(failure_types.items())),
            *(["| - | 0 |"] if not failure_types else []),
            "",
            "## Failure Stage Distribution",
            "",
            "| Failure Stage | Count |",
            "| --- | ---: |",
            *(f"| {name} | {count} |" for name, count in sorted(failure_stages.items())),
            *(["| - | 0 |"] if not failure_stages else []),
            "",
            "## Task Results",
            "",
            "| Task | Result | Steps | Tokens | Failure Type | Failure Stage | Failure Message | Source Repo | Workspace Repo | Patch | Artifact |",
            "| --- | --- | ---: | ---: | --- | --- | --- | --- | --- | --- | --- |",
            *[
                f"| `{Path(item.get('task_file') or item.get('artifact_dir')).name}` | "
                f"{'success' if item.get('task_success') else 'failed'} | "
                f"{item.get('steps_taken')} | "
                f"{item.get('total_tokens')} | "
                f"{item.get('failure_type') or '-'} | "
                f"{item.get('failure_stage') or '-'} | "
                f"{_markdown_cell(item.get('failure_message'))} | "
                f"{_markdown_path_cell(item.get('repo_source'))} | "
                f"{_markdown_path_cell(item.get('workspace_repo'))} | "
                f"{f'`{Path(item['patch_path']).name}`' if item.get('patch_path') else '-'} | "
                f"`{Path(item['artifact_dir']).name}` |"
                for item in summary.get("runs", [])
            ],
            "",
        ]
    )


def _render_ablation_markdown(grouped: dict) -> str:
    groups = grouped.get("groups", {})
    lines = [
        "# Ablation Report",
        "",
        f"- Artifact root: `{grouped.get('artifact_root')}`",
        f"- Scope: `{'all-runs' if grouped.get('include_preverified', True) else 'agent-only'}`",
        "",
        "| Profile | Runs | Success Rate | First-pass | Avg Steps | Avg Tools | Avg Tokens | Patch Success | Failure Analyses | Edit Plans | Patch Review Fails |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, summary in sorted(groups.items()):
        lines.append(
            f"| {name} | {summary['run_count']} | {summary['success_rate']:.2%} | "
            f"{summary['first_pass_success_rate']:.2%} | {summary['avg_steps']} | "
            f"{summary['avg_tool_calls']} | {summary['avg_tokens']} | "
            f"{summary['patch_success_rate']:.2%} | {summary['failure_analyses']} | "
            f"{summary['edit_plans']} | {summary['patch_review_failures']} |"
        )
    if not groups:
        lines.append("| - | 0 | 0.00% | 0.00% | 0 | 0 | 0 | 0.00% | 0 | 0 | 0 |")
    return "\n".join(lines) + "\n"


def _markdown_cell(value: object) -> str:
    if value is None:
        return "-"
    text = str(value).replace("\n", "<br>").replace("|", "\\|").strip()
    return text or "-"


def _markdown_path_cell(value: object) -> str:
    if value is None:
        return "-"
    return f"`{_markdown_cell(value)}`"


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main() -> None:
    cli(obj={})


if __name__ == "__main__":
    main()
