"""
agent/core.py

ReAct 主循环。整个 agent 的大脑。

职责（只做这些，不做别的）：
- 维护对话历史，每轮组装 messages 调用 LLM
- 拿到 Action 后调用 ToolRegistry 执行
- 把 Action + Observation 写入 EventLog
- 检测三种终止/Reflection 触发条件
- 返回 RunResult

不负责：
- 任何 LLM 细节（交给 LLMBackend）
- 任何工具实现（交给 Tool）
- 上下文压缩（由 context/ 模块负责）
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from agent.event_log import EventLog
from agent.failure import (
    FAILURE_STAGE_AGENT_LOOP,
    classify_exception_failure,
    failure,
    infer_failure_from_event_dicts,
    normalize_agent_loop_failure,
)
from agent.memory import format_memory_hits, search_memories
from context.history import ConversationHistory
from context.repo_map import RepoMap
from context.token_budget import TokenBudget
from agent.prompt import (
    build_system_prompt,
    build_task_prompt,
    recovery_prompt_for_failure,
    reflection_no_edit,
    reflection_test_failed,
    reflection_verification_failed,
)
from agent.review import review_patch_before_finish
from agent.task import (
    Action, ActionType, Event, EventType,
    Observation, ObservationStatus, RunResult, RunStatus, Task, ToolCall,
)
from llm.base import LLMBackend, LLMMessage, LLMToolSchema
from tools.base import ToolRegistry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass
class AgentConfig:
    """Agent 运行时配置，从 config/default.yaml 加载后传入。"""
    max_steps: int = 40
    reflection_no_edit_steps: int = 6   # 连续 N 步无文件写操作触发 Reflection
    loop_detection_window: int = 3       # 连续 N 步完全相同 action 判定死循环
    test_tool_names: tuple[str, ...] = ("test", "pytest")  # 触发 Reflection 的工具名
    budget_tokens: int = 80_000            # 总 token 预算
    history_max_messages: int = 40         # 历史最大条数
    llm_max_retries: int = 3               # LLM 调用失败最大重试次数
    llm_retry_delay: float = 2.0           # 重试间隔（秒，指数退避）
    auto_graph_probe_on_test_failure: bool = True   # 测试失败后自动探测图邻居
    auto_graph_probe_limit: int = 2                # 最多自动探测的候选文件数
    auto_file_prefetch_on_test_failure: bool = True  # 测试失败后自动预读第一候选文件
    auto_symbol_probe_on_test_failure: bool = True  # 测试失败后自动定位相关函数/类定义
    verify_on_finish: bool = True                   # FINISH 前用 task.test_cmd 做最后验证
    self_review_on_finish: bool = True              # FINISH 前检查 diff 中的明显阻断问题
    taxonomy_recovery_prompts: bool = True          # 根据 failure_type 注入恢复提示
    enable_context_compression: bool = True         # 历史超窗时压缩旧消息
    enable_long_memory: bool = True                 # 运行开始时检索本地长期记忆
    long_memory_limit: int = 5                      # 每次注入的长期记忆条数上限
    log_dir: str = "./logs"                         # memory/artifact 默认日志目录
    stream: bool = False                   # 是否启用流式输出
    stream_callback: object = None         # StreamCallback，最终回答流式回调
    thought_callback: object = None        # StreamCallback，推理过程流式回调（推理模型专用）
    confirm_dangerous: bool = False        # 是否对危险命令要求用户确认
    confirm_callback: object = None        # ConfirmCallback，None=跳过确认



# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class Agent:
    """
    ReAct 主循环实现。

    用法：
        agent = Agent(backend, registry, config)
        result = agent.run(task, log)
    """

    def __init__(
        self,
        backend: LLMBackend,
        registry: ToolRegistry,
        config: AgentConfig | None = None,
    ) -> None:
        self._backend = backend
        self._registry = registry
        self._cfg = config or AgentConfig()

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def run(self, task: Task, log: EventLog) -> RunResult:
        """
        执行一次完整的 agent 运行。

        Args:
            task: 任务描述
            log:  已初始化的 EventLog（由调用方创建并传入）

        Returns:
            RunResult，包含最终状态和统计信息
        """
        self._current_repo_path = task.repo_path
        # 按 repo_path 隔离 repo_map 缓存，换 repo 时自动重建
        cache_key = (task.repo_path, tuple(task.exclude_paths))
        if getattr(self, "_repo_map_cache_key", None) != cache_key:
            if hasattr(self, "_repo_map_cache"):
                del self._repo_map_cache
            if hasattr(self, "_repo_map_trace_cache"):
                del self._repo_map_trace_cache
            self._repo_map_cache_key = cache_key
        log.log_task_start(task)
        logger.info("Agent starting task %s", task.task_id)

        # 初始化上下文管理器
        # 如果调用方（ChatSession）注入了共享 history，直接复用；
        # 否则新建（单次 run 模式）
        if hasattr(self, "_pending_history") and self._pending_history is not None:
            history = self._pending_history
        else:
            history = ConversationHistory(
                max_messages=self._cfg.history_max_messages,
                enable_compression=self._cfg.enable_context_compression,
            )
            # 单次模式：把任务描述作为第一条 user 消息
            from agent.prompt import build_task_prompt
            history.add(LLMMessage(
                role="user",
                content=build_task_prompt(
                    task.description,
                    task.repo_path,
                    task.issue_url,
                    test_cmd=task.test_cmd,
                    exclude_paths=task.exclude_paths,
                    target_files=task.target_files,
                ),
            ))
        self._inject_long_memory(task, log, history)
        token_budget = TokenBudget(total=self._cfg.budget_tokens)
        repo_map = RepoMap(task.repo_path, exclude_paths=task.exclude_paths)
        repo_map_budget = token_budget.default_plan().repo_map
        if not hasattr(self, "_repo_map_cache"):
            self._repo_map_cache = repo_map.build(budget=repo_map_budget)
            self._repo_map_trace_cache = repo_map.last_trace()
        log.log_repo_map(
            summary=self._repo_map_cache,
            trace=getattr(self, "_repo_map_trace_cache", repo_map.last_trace()),
            budget=repo_map_budget,
        )

        total_tokens = 0
        steps_without_edit = 0
        edits_made = False
        logged_compressed_count = history.compressed_message_count

        for step in range(1, task.max_steps + 1):
            logger.debug("Step %d/%d", step, task.max_steps)

            # ── 1. 组装 messages，调用 LLM ──────────────────────────────
            if history.compressed_message_count > logged_compressed_count:
                log.log_reflection(
                    step=step,
                    reason="context_compression",
                    prompt=history.compressed_summary,
                )
                logged_compressed_count = history.compressed_message_count
            messages = self._build_messages(history, token_budget, repo_map)
            tools = self._registry.get_schemas()

            try:
                response = self._call_with_retry(messages, tools)
            except Exception as exc:
                logger.error("LLM call failed at step %d after retries: %s", step, exc)
                failure_info = failure(
                    f"LLM error: {exc}",
                    failure_type=classify_exception_failure(exc),
                    failure_stage=FAILURE_STAGE_AGENT_LOOP,
                )
                log.log_task_failed(steps=step, **failure_info.to_dict())
                return RunResult(
                    task_id=task.task_id,
                    status=RunStatus.FAILED,
                    summary=f"LLM call failed: {exc}",
                    steps_taken=step,
                    total_tokens=total_tokens,
                    error=str(exc),
                    failure_type=failure_info.failure_type,
                    failure_stage=failure_info.failure_stage,
                    failure_message=failure_info.failure_message,
                )

            total_tokens += response.total_tokens
            action = response.action

            # ── 2. 写入 Action event ────────────────────────────────────
            log.log_action(step=step, action=action, raw_content=response.raw_content)
            logger.info("Step %d: %r", step, action)

            # ── 3. 检测死循环（连续相同 action）────────────────────────
            if self._is_looping(log):
                reason = f"Loop detected: same action repeated {self._cfg.loop_detection_window} times"
                logger.warning(reason)
                failure_info = self._infer_failure_from_log(log, reason)
                log.log_task_failed(steps=step, **failure_info.to_dict())
                return RunResult(
                    task_id=task.task_id,
                    status=RunStatus.GAVE_UP,
                    summary=reason,
                    steps_taken=step,
                    total_tokens=total_tokens,
                    failure_type=failure_info.failure_type,
                    failure_stage=failure_info.failure_stage,
                    failure_message=failure_info.failure_message,
                )

            # ── 4. 终止 action ──────────────────────────────────────────
            if action.action_type == ActionType.FINISH:
                summary = action.message or "Task complete."
                patch = self._get_git_diff(task.repo_path)
                if self._should_continue_after_self_review(
                    task=task,
                    step=step,
                    patch=patch,
                    log=log,
                    history=history,
                ):
                    continue
                if self._should_continue_after_finish_verification(
                    task=task,
                    step=step,
                    log=log,
                    history=history,
                ):
                    continue
                log.log_task_complete(steps=step, summary=summary)
                return RunResult(
                    task_id=task.task_id,
                    status=RunStatus.SUCCESS,
                    summary=summary,
                    steps_taken=step,
                    total_tokens=total_tokens,
                    patch=patch,
                )

            if action.action_type == ActionType.GIVE_UP:
                reason = action.message or "Agent gave up."
                failure_info = self._infer_failure_from_log(log, reason)
                log.log_task_failed(steps=step, **failure_info.to_dict())
                return RunResult(
                    task_id=task.task_id,
                    status=RunStatus.GAVE_UP,
                    summary=reason,
                    steps_taken=step,
                    total_tokens=total_tokens,
                    failure_type=failure_info.failure_type,
                    failure_stage=failure_info.failure_stage,
                    failure_message=failure_info.failure_message,
                )

            # ── 5. 执行工具 ─────────────────────────────────────────────
            if action.action_type == ActionType.TOOL_CALL and action.tool_call:
                tc = action.tool_call
                result = self._registry.execute_tool(tc.name, tc.params)
                observation = result.to_observation(tc.name)

                # 追踪是否有文件写操作
                if tc.name in ("file_write", "file_edit", "edit", "apply_patch"):
                    steps_without_edit = 0
                    edits_made = True
                else:
                    steps_without_edit += 1

                log.log_observation(step=step, observation=observation)

                # 把 action 和 observation 加入对话历史
                history.add(LLMMessage(
                    role="assistant",
                    content=self._format_action_for_history(action),
                ))
                history.add(LLMMessage(
                    role="user",
                    content=self._format_observation_for_history(observation),
                ))

                self._maybe_inject_taxonomy_recovery(
                    step=step,
                    observation=observation,
                    log=log,
                    history=history,
                    skip_for_test_failure=tc.name in self._cfg.test_tool_names,
                )

                if self._should_finish_after_verification(
                    task,
                    tool_call=tc,
                    observation=observation,
                    edits_made=edits_made,
                ):
                    summary = (
                        "Target verification already passes and no code changes were needed. "
                        "The current code already satisfies the requested fix."
                    )
                    log.log_task_complete(steps=step, summary=summary)
                    return RunResult(
                        task_id=task.task_id,
                        status=RunStatus.SUCCESS,
                        summary=summary,
                        steps_taken=step,
                        total_tokens=total_tokens,
                        patch=self._get_git_diff(task.repo_path),
                    )

                # ── 6. Reflection 触发判断 ──────────────────────────────

                # 触发条件 A：测试工具失败
                if (
                    tc.name in self._cfg.test_tool_names
                    and not observation.is_success()
                ):
                    self._auto_probe_graph_neighbors(
                        task=task,
                        step=step,
                        tool_call=tc,
                        observation=observation,
                        log=log,
                        history=history,
                    )
                    self._auto_probe_symbols(
                        task=task,
                        step=step,
                        observation=observation,
                        log=log,
                        history=history,
                    )
                    reflect_prompt = reflection_test_failed()
                    graph_hint = self._build_graph_hint(task, tc, observation)
                    if graph_hint:
                        reflect_prompt = f"{reflect_prompt}\n\n{graph_hint}"
                    log.log_reflection(
                        step=step,
                        reason="test_failed",
                        prompt=reflect_prompt,
                    )
                    history.add(LLMMessage(role="user", content=reflect_prompt))
                    logger.debug("Reflection triggered: test_failed at step %d", step)

                # 触发条件 B：连续 N 步无编辑
                elif steps_without_edit >= self._cfg.reflection_no_edit_steps:
                    reflect_prompt = reflection_no_edit(steps_without_edit)
                    log.log_reflection(
                        step=step,
                        reason="no_edit",
                        prompt=reflect_prompt,
                    )
                    history.add(LLMMessage(role="user", content=reflect_prompt))
                    steps_without_edit = 0  # 重置计数，避免每步都触发
                    logger.debug("Reflection triggered: no_edit at step %d", step)

            elif action.action_type == ActionType.REFLECTION:
                # LLM 主动要求 reflection（预留，当前 MockBackend 不产生）
                history.add(LLMMessage(
                    role="assistant",
                    content=action.thought,
                ))

        # ── 7. 超出步数上限 ─────────────────────────────────────────────
        reason = f"Reached max_steps limit ({task.max_steps})"
        failure_info = self._infer_failure_from_log(log, reason)
        log.log_task_failed(steps=task.max_steps, **failure_info.to_dict())
        return RunResult(
            task_id=task.task_id,
            status=RunStatus.MAX_STEPS,
            summary=reason,
            steps_taken=task.max_steps,
            total_tokens=total_tokens,
            failure_type=failure_info.failure_type,
            failure_stage=failure_info.failure_stage,
            failure_message=failure_info.failure_message,
        )

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _build_messages(
        self,
        history: ConversationHistory,
        token_budget: TokenBudget,
        repo_map: RepoMap,
    ) -> list[LLMMessage]:
        """
        组装发给 LLM 的完整 messages，含 token 裁剪。
        """
        schemas = self._registry.get_schemas()

        system_content = build_system_prompt(
            repo_path=getattr(self, "_current_repo_path", "."),
            tools=schemas,
            repo_summary=self._repo_map_cache,
        )

        # 裁剪历史
        trimmed_history_dicts = token_budget.trim_history(
            history.to_dicts(),
            token_budget.default_plan().history,
        )

        # 组装：system + 裁剪后的 history
        messages = [LLMMessage(role="system", content=system_content)]
        for d in trimmed_history_dicts:
            messages.append(LLMMessage(role=d["role"], content=d["content"]))
        return messages

    def _inject_long_memory(
        self,
        task: Task,
        log: EventLog,
        history: ConversationHistory,
    ) -> None:
        if not self._cfg.enable_long_memory or self._cfg.long_memory_limit <= 0:
            return
        hits = search_memories(
            self._cfg.log_dir,
            query=task.description,
            repo_path=task.source_repo_path or task.repo_path,
            limit=self._cfg.long_memory_limit,
        )
        memory_text = format_memory_hits(hits)
        if not memory_text:
            return
        log.log_reflection(step=0, reason="long_memory", prompt=memory_text)
        history.add(LLMMessage(role="user", content=memory_text))

    def _format_action_for_history(self, action: Action) -> str:
        """把 Action 格式化为 assistant 消息，写入对话历史。"""
        parts = [f"Thought: {action.thought}"]
        if action.tool_call:
            parts.append(f"Action: {action.tool_call.name}")
            parts.append(f"Params: {json.dumps(action.tool_call.params, ensure_ascii=False)}")
        elif action.message:
            parts.append(f"Message: {action.message}")
        return "\n".join(parts)

    def _format_observation_for_history(self, observation: Observation) -> str:
        """把 Observation 格式化为 user 消息，写入对话历史。"""
        status = "SUCCESS" if observation.is_success() else "ERROR"
        lines = [f"[Tool: {observation.tool_name} | {status}]"]
        if observation.output:
            lines.append(observation.output)
        if observation.error and not observation.is_success():
            lines.append(f"Error: {observation.error}")
        return "\n".join(lines)

    def _is_looping(self, log: EventLog) -> bool:
        """
        检测是否陷入死循环：最近 N 条 action 完全相同。
        比较 (tool_name, params) 元组。
        """
        n = self._cfg.loop_detection_window
        actions = log.get_actions()
        if len(actions) < n:
            return False

        recent = actions[-n:]
        # 只对 TOOL_CALL 类型做检测
        if not all(a.action_type == ActionType.TOOL_CALL for a in recent):
            return False
        if not all(a.tool_call for a in recent):
            return False

        first = recent[0].tool_call
        return all(
            a.tool_call.name == first.name and a.tool_call.params == first.params
            for a in recent[1:]
        )

    def _should_continue_after_self_review(
        self,
        *,
        task: Task,
        step: int,
        patch: str | None,
        log: EventLog,
        history: ConversationHistory,
    ) -> bool:
        if not self._cfg.self_review_on_finish:
            return False
        review = review_patch_before_finish(patch or self._collect_recent_patch_text(log))
        observation = Observation(
            status=ObservationStatus.SUCCESS if review.success else ObservationStatus.ERROR,
            output=review.message,
            tool_name="self_review",
            error=None if review.success else review.message,
            metadata=review.to_metadata(),
        )
        log.log_observation(step=step, observation=observation)
        if review.success:
            return False

        history.add(LLMMessage(role="assistant", content="Finish requested, but self-review found blocking issues."))
        history.add(LLMMessage(role="user", content=self._format_observation_for_history(observation)))
        prompt = (
            "[REFLECTION] Self-review found blocking issues in the diff. "
            "Fix these findings before finishing:\n"
            + "\n".join(f"- {finding}" for finding in review.findings)
        )
        log.log_reflection(step=step, reason="self_review_failed", prompt=prompt)
        history.add(LLMMessage(role="user", content=prompt))
        return True

    def _collect_recent_patch_text(self, log: EventLog) -> str | None:
        latest: str | None = None
        for event in log.replay():
            if event.event_type != EventType.OBSERVATION:
                continue
            obs = event.payload.get("observation", {})
            metadata = obs.get("metadata") or {}
            patch = metadata.get("patch") or {}
            if isinstance(patch, dict):
                for key in ("content", "replace"):
                    value = patch.get(key)
                    if isinstance(value, str):
                        latest = value
        return latest

    def _should_continue_after_finish_verification(
        self,
        *,
        task: Task,
        step: int,
        log: EventLog,
        history: ConversationHistory,
    ) -> bool:
        if not self._cfg.verify_on_finish or not task.test_cmd or "shell" not in self._registry:
            return False
        result = self._registry.execute_tool(
            "shell",
            {"cmd": task.test_cmd, "cwd": task.repo_path, "timeout": 120},
        )
        if result.metadata is None:
            result.metadata = {}
        result.metadata["verification_cmd"] = task.test_cmd
        result.metadata["cwd"] = task.repo_path
        observation = result.to_observation("finish_verifier")
        log.log_observation(step=step, observation=observation)
        if observation.is_success():
            return False

        history.add(LLMMessage(role="assistant", content="Finish requested, but target verification failed."))
        history.add(LLMMessage(role="user", content=self._format_observation_for_history(observation)))
        reflect_prompt = reflection_verification_failed()
        log.log_reflection(step=step, reason="finish_verification_failed", prompt=reflect_prompt)
        history.add(LLMMessage(role="user", content=reflect_prompt))
        return True

    def _maybe_inject_taxonomy_recovery(
        self,
        *,
        step: int,
        observation: Observation,
        log: EventLog,
        history: ConversationHistory,
        skip_for_test_failure: bool,
    ) -> None:
        if not self._cfg.taxonomy_recovery_prompts or observation.is_success() or skip_for_test_failure:
            return
        prompt = recovery_prompt_for_failure((observation.metadata or {}).get("failure_type"))
        if not prompt:
            return
        log.log_reflection(step=step, reason="taxonomy_recovery", prompt=prompt)
        history.add(LLMMessage(role="user", content=prompt))

    def _call_with_retry(
        self,
        messages: list[LLMMessage],
        tools: list[LLMToolSchema],
    ):
        """
        带指数退避重试的 LLM 调用。
        stream=True 时走 backend.stream()，否则走 complete()。
        不重试：认证失败（401/403）、参数错误（400）。
        """
        import time as _time

        last_exc: Exception | None = None
        delay = self._cfg.llm_retry_delay

        for attempt in range(1, self._cfg.llm_max_retries + 1):
            try:
                if self._cfg.stream:
                    cb = self._cfg.stream_callback
                    thought_cb = self._cfg.thought_callback
                    if hasattr(self._backend, "stream"):
                        return self._backend.stream(
                            messages, tools,
                            on_text=cb,
                            on_thought=thought_cb,
                        )
                return self._backend.complete(messages, tools)
            except Exception as exc:
                last_exc = exc
                exc_str = str(exc).lower()
                if any(kw in exc_str for kw in (
                    "401", "403", "invalid api key", "authentication",
                    "400", "bad request",
                )):
                    raise
                if attempt < self._cfg.llm_max_retries:
                    logger.warning(
                        "LLM call failed (attempt %d/%d): %s — retrying in %.1fs",
                        attempt, self._cfg.llm_max_retries, exc, delay,
                    )
                    _time.sleep(delay)
                    delay *= 2

        raise last_exc  # type: ignore[misc]

    def _get_git_diff(self, repo_path: str) -> str | None:
        """抓取 git diff HEAD 作为 patch，失败时静默返回 None。"""
        import subprocess
        try:
            proc = subprocess.run(
                ["git", "diff", "HEAD"],
                capture_output=True, text=True, timeout=10, cwd=repo_path,
            )
            diff = proc.stdout.strip()
            return diff if diff else None
        except Exception:
            return None

    def _should_finish_after_verification(
        self,
        task: Task,
        tool_call: ToolCall,
        observation: Observation,
        edits_made: bool,
    ) -> bool:
        """
        Allow a conservative early finish for benchmark-like tasks that are already fixed.

        Only triggers when:
        - caller opted in via task.finish_if_verified
        - no edit has been made in this run
        - the successful tool call is a targeted test run
        """
        if not task.finish_if_verified or edits_made:
            return False
        if tool_call.name not in self._cfg.test_tool_names:
            return False
        if not observation.is_success():
            return False
        return self._matches_verification_target(task, tool_call)

    def _matches_verification_target(self, task: Task, tool_call: ToolCall) -> bool:
        if not task.test_cmd:
            return False
        params = tool_call.params or {}
        path = str(params.get("path", "")).strip()
        cwd = str(params.get("cwd", "")).strip()
        test_cmd = task.test_cmd

        if path and path in test_cmd:
            return True
        if cwd and cwd in test_cmd:
            return True
        if not path and not cwd:
            return False
        return False

    def _build_graph_hint(
        self,
        task: Task,
        tool_call: ToolCall,
        observation: Observation,
    ) -> str | None:
        trace = getattr(self, "_repo_map_trace_cache", {}) or {}
        chunks = trace.get("chunks") or []
        if not chunks:
            return None

        candidate_paths = self._candidate_graph_paths(task, tool_call, observation)
        if not candidate_paths:
            return None

        matched_chunks = []
        seen_paths: set[str] = set()
        for candidate in candidate_paths:
            normalized = candidate.replace("\\", "/")
            for chunk in chunks:
                path = str(chunk.get("path", ""))
                if not path or path in seen_paths:
                    continue
                if (
                    path == normalized
                    or path.endswith(f"/{normalized}")
                    or normalized.endswith(f"/{path}")
                ):
                    matched_chunks.append(chunk)
                    seen_paths.add(path)

        if not matched_chunks:
            return None

        ranked_related = self._prioritize_target_files(
            task.target_files,
            self._rank_graph_related_chunks(matched_chunks, chunks),
        )

        lines = [
            "[GRAPH HINT] Related repository graph context for the failing target:",
        ]
        for chunk in matched_chunks[:2]:
            path = chunk.get("path", "(unknown)")
            imports = list(chunk.get("imports") or [])[:3]
            imported_by = list(chunk.get("imported_by") or [])[:3]
            referenced = list(chunk.get("referenced_symbols") or [])[:3]
            lines.append(f"- {path}")
            if imports:
                lines.append(f"  imports -> {', '.join(imports)}")
            if imported_by:
                lines.append(f"  imported by <- {', '.join(imported_by)}")
            if referenced:
                refs = ", ".join(
                    f"{item.get('name')}@{item.get('path')}"
                    for item in referenced
                    if item.get("name") and item.get("path")
                )
                if refs:
                    lines.append(f"  symbol refs -> {refs}")

        if ranked_related:
            lines.append("Likely related files to inspect next:")
            for index, item in enumerate(ranked_related[:4], start=1):
                reason = item["reason"]
                lines.append(f"{index}. {item['path']} — {reason}")

        lines.append(
            "Use graph_neighbors on one of these files or a relevant symbol before making a broad change."
        )
        return "\n".join(lines)

    def _auto_probe_graph_neighbors(
        self,
        *,
        task: Task,
        step: int,
        tool_call: ToolCall,
        observation: Observation,
        log: EventLog,
        history: ConversationHistory,
    ) -> None:
        if not self._cfg.auto_graph_probe_on_test_failure:
            return
        if "graph_neighbors" not in self._registry:
            return

        trace = getattr(self, "_repo_map_trace_cache", {}) or {}
        chunks = trace.get("chunks") or []
        if not chunks:
            return

        candidate_paths = self._candidate_graph_paths(task, tool_call, observation)
        if not candidate_paths:
            return

        matched_chunks = []
        seen_paths: set[str] = set()
        for candidate in candidate_paths:
            normalized = candidate.replace("\\", "/")
            for chunk in chunks:
                path = str(chunk.get("path", ""))
                if not path or path in seen_paths:
                    continue
                if (
                    path == normalized
                    or path.endswith(f"/{normalized}")
                    or normalized.endswith(f"/{path}")
                ):
                    matched_chunks.append(chunk)
                    seen_paths.add(path)

        if not matched_chunks:
            return

        ranked_related = self._prioritize_target_files(
            task.target_files,
            self._rank_graph_related_chunks(matched_chunks, chunks),
        )
        self._auto_prefetch_related_file(
            task=task,
            ranked_related=ranked_related,
            step=step,
            log=log,
            history=history,
        )
        probe_targets = [item["path"] for item in ranked_related[: self._cfg.auto_graph_probe_limit]]
        if not probe_targets:
            probe_targets = [str(chunk.get("path")) for chunk in matched_chunks[:1] if chunk.get("path")]

        for target in probe_targets:
            result = self._registry.execute_tool(
                "graph_neighbors",
                {"path": target, "root": task.repo_path},
            )
            if result.metadata is None:
                result.metadata = {}
            result.metadata["auto_graph_probe"] = True
            result.metadata["probe_target"] = target
            probe_observation = result.to_observation("graph_neighbors")
            log.log_observation(step=step, observation=probe_observation)
            history.add(LLMMessage(
                role="user",
                content=self._format_observation_for_history(probe_observation),
            ))

    def _auto_prefetch_related_file(
        self,
        *,
        task: Task,
        ranked_related: list[dict[str, str]],
        step: int,
        log: EventLog,
        history: ConversationHistory,
    ) -> None:
        if not self._cfg.auto_file_prefetch_on_test_failure:
            return
        if "file_read" not in self._registry:
            return
        if not ranked_related:
            return

        target = task.target_files[0] if task.target_files else ranked_related[0]["path"]
        result = self._registry.execute_tool(
            "file_read",
            {"path": str(Path(task.repo_path) / target)},
        )
        if result.metadata is None:
            result.metadata = {}
        result.metadata["auto_file_prefetch"] = True
        result.metadata["prefetch_target"] = target
        prefetch_observation = result.to_observation("file_read")
        log.log_observation(step=step, observation=prefetch_observation)
        history.add(LLMMessage(
            role="user",
            content=self._format_observation_for_history(prefetch_observation),
        ))

    def _auto_probe_symbols(
        self,
        *,
        task: Task,
        step: int,
        observation: Observation,
        log: EventLog,
        history: ConversationHistory,
    ) -> None:
        if not self._cfg.auto_symbol_probe_on_test_failure:
            return
        if "find_symbol" not in self._registry:
            return
        symbols = self._candidate_symbol_names(observation)
        if not symbols:
            return
        for symbol in symbols[:2]:
            result = self._registry.execute_tool(
                "find_symbol",
                {"symbol": symbol, "path": task.repo_path},
            )
            if result.metadata is None:
                result.metadata = {}
            result.metadata["auto_symbol_probe"] = True
            result.metadata["probe_symbol"] = symbol
            symbol_observation = result.to_observation("find_symbol")
            log.log_observation(step=step, observation=symbol_observation)
            history.add(LLMMessage(
                role="user",
                content=self._format_observation_for_history(symbol_observation),
            ))

    def _candidate_symbol_names(self, observation: Observation) -> list[str]:
        text = "\n".join(part for part in [observation.output, observation.error or ""] if part)
        candidates: list[str] = []
        patterns = [
            r"NameError: name ['\"]([A-Za-z_]\w*)['\"]",
            r"AttributeError: .*['\"]([A-Za-z_]\w*)['\"]",
            r"FAILED [\w./-]+::([A-Za-z_]\w*)",
            r"in ([A-Za-z_]\w*)\n",
        ]
        for pattern in patterns:
            candidates.extend(re.findall(pattern, text))
        ignored = {"test", "assert", "self", "None", "True", "False"}
        deduped: list[str] = []
        seen: set[str] = set()
        for symbol in candidates:
            if symbol in ignored or symbol in seen:
                continue
            deduped.append(symbol)
            seen.add(symbol)
        return deduped

    def _candidate_graph_paths(
        self,
        task: Task,
        tool_call: ToolCall,
        observation: Observation,
    ) -> list[str]:
        candidates: list[str] = []
        params = tool_call.params or {}
        for key in ("path", "cwd"):
            value = str(params.get(key, "")).strip()
            if value.endswith(".py"):
                candidates.append(value)

        if task.test_cmd:
            candidates.extend(re.findall(r"[\w./-]+\.py", task.test_cmd))
        if observation.output:
            candidates.extend(re.findall(r"[\w./-]+\.py", observation.output))
        if observation.error:
            candidates.extend(re.findall(r"[\w./-]+\.py", observation.error))

        deduped: list[str] = []
        seen: set[str] = set()
        for item in task.target_files:
            normalized = item.strip().strip("'\"")
            if normalized and normalized not in seen:
                deduped.append(normalized)
                seen.add(normalized)
        for item in candidates:
            normalized = item.strip().strip("'\"")
            if not normalized or normalized in seen:
                continue
            deduped.append(normalized)
            seen.add(normalized)
        return deduped

    def _rank_graph_related_chunks(
        self,
        source_chunks: list[dict[str, object]],
        all_chunks: list[dict[str, object]],
    ) -> list[dict[str, str]]:
        chunk_by_path = {
            str(chunk.get("path")): chunk
            for chunk in all_chunks
            if chunk.get("path")
        }
        source_paths = {str(chunk.get("path")) for chunk in source_chunks if chunk.get("path")}
        scored: dict[str, dict[str, object]] = {}

        def _add_score(path: str, score: float, reason: str) -> None:
            if not path or path in source_paths:
                return
            current = scored.get(path)
            if current is None or score > float(current["score"]):
                scored[path] = {"path": path, "score": score, "reason": reason}

        for chunk in source_chunks:
            source_path = str(chunk.get("path", ""))
            for imported in chunk.get("imports") or []:
                _add_score(str(imported), 3.0, f"imported by {source_path}")
            for importer in chunk.get("imported_by") or []:
                _add_score(str(importer), 2.8, f"imports {source_path}")
            for ref in chunk.get("referenced_symbols") or []:
                if isinstance(ref, dict) and ref.get("path") and ref.get("name"):
                    _add_score(str(ref["path"]), 2.5, f"defines referenced symbol {ref['name']}")
            for ref_by in chunk.get("referenced_by") or []:
                _add_score(str(ref_by), 2.0, f"references symbols from {source_path}")

        first_hop_paths = [path for path in scored.keys() if path in chunk_by_path]
        for path in first_hop_paths:
            hop_chunk = chunk_by_path[path]
            for imported in hop_chunk.get("imports") or []:
                _add_score(str(imported), 1.7, f"imported by {path}")
            for ref in hop_chunk.get("referenced_symbols") or []:
                if isinstance(ref, dict) and ref.get("path") and ref.get("name"):
                    _add_score(str(ref["path"]), 1.5, f"defines referenced symbol {ref['name']} for {path}")

        ordered = sorted(
            scored.values(),
            key=lambda item: (-float(item["score"]), str(item["path"])),
        )
        return [
            {"path": str(item["path"]), "reason": str(item["reason"])}
            for item in ordered
            if str(item["path"]) in chunk_by_path
        ]

    def _prioritize_target_files(
        self,
        target_files: list[str],
        ranked_related: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        if not target_files or not ranked_related:
            if not target_files:
                return ranked_related
            existing = {item["path"] for item in ranked_related}
            synthetic = [
                {"path": path, "reason": "explicit task target"}
                for path in target_files
                if path not in existing
            ]
            return synthetic + ranked_related
        target_set = set(target_files)
        existing = {item["path"] for item in ranked_related}
        synthetic = [
            {"path": path, "reason": "explicit task target"}
            for path in target_files
            if path not in existing
        ]
        preferred = [item for item in ranked_related if item["path"] in target_set]
        remaining = [item for item in ranked_related if item["path"] not in target_set]
        return synthetic + preferred + remaining

    def _infer_failure_from_log(self, log: EventLog, reason: str):
        inferred = infer_failure_from_event_dicts(
            [event.to_dict() for event in log.replay()],
            default_stage=FAILURE_STAGE_AGENT_LOOP,
        )
        if inferred is not None:
            return failure(
                reason,
                failure_type=inferred.failure_type,
                failure_stage=inferred.failure_stage,
                failure_message=inferred.failure_message,
            )
        return failure(
            reason,
            failure_type=normalize_agent_loop_failure(reason),
            failure_stage=FAILURE_STAGE_AGENT_LOOP,
        )
