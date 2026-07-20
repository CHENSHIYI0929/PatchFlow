# PatchFlow 使用指南

本文是 PatchFlow 的操作手册。项目概览和最短上手路径见 [README.md](README.md)。

## 1. 安装与配置

要求 Python 3.11+。

```bash
git clone <repo-url> PatchFlow
cd PatchFlow
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
agent --help
```

默认配置在 `config/default.yaml`：

```yaml
llm:
  provider: openai
  model: deepseek-ai/DeepSeek-V4-Flash
  api_key: ${SILICONFLOW_API_KEY}
  base_url: https://api.siliconflow.cn/v1
  max_tokens: 8192

agent:
  max_steps: 40
  budget_tokens: 80000
  log_dir: ./logs

context:
  repo_map_budget: 8000
  history_window: 20
  enable_compression: true
  enable_long_memory: true
  long_memory_limit: 5
```

设置对应服务商的环境变量：

```bash
export SILICONFLOW_API_KEY=sk-xxx
# 或 ANTHROPIC_API_KEY / OPENAI_API_KEY
```

验证：

```bash
python scripts/smoke_test.py
```

可选安装更多语言 parser 和精确 token 计数：

```bash
pip install -e ".[full]"
```

## 2. Chat 模式

```bash
agent chat
agent chat --repo /path/to/project
agent chat --model deepseek-ai/DeepSeek-V4-Flash
agent chat --sandbox
```

对话命令：

- `/stats`：查看本轮统计
- `/clear`：清空历史，保留初始上下文
- `/help`：帮助
- `/exit`：退出

Chat 适合连续探索、分步修复和追问。危险命令默认要求确认。

## 3. Run 模式

```bash
agent run --repo /path/to/project --task "修复 tests/test_parser.py 的失败"
agent run --repo /path/to/project --task-file task.txt
agent run --task "..." --model <model> --max-steps 20
agent run --task "..." --confirm
agent run --task "..." --sandbox
```

执行模式：

```bash
agent run --task "分析潜在风险" --run-mode safe
agent run --task "修复问题" --run-mode review --confirm
agent run --task "修复问题" --run-mode auto
```

| 模式 | 行为 |
| --- | --- |
| `safe` | 只分析和计划，不写文件 |
| `review` | 生成 patch preview，确认后应用 |
| `auto` | 自动编辑、验证和结束，默认模式 |

可单独关闭机制用于排查或消融：

```bash
agent run --task "..." --disable-failure-analyzer
agent run --task "..." --disable-hybrid-retrieval
agent run --task "..." --disable-edit-plan
agent run --task "..." --disable-self-review
agent run --task "..." --disable-long-memory --disable-compression
```

## 4. Benchmark

### 任务格式

每个 `.txt` 文件是一项任务：

```text
---
repo: benchmark_fixtures/run_demo
test_path: test_report.py
lint_cmd: python -m ruff check .
patch_policy_cmd: python scripts/check_patch_policy.py
target_files: report.py, scores.py
exclude_paths: logs, README.md
category: bugfix
difficulty: medium
expected_failure_type: verification_failed
max_steps: 12
finish_if_verified: true
skip_preverified: true
---
修复成绩报告生成逻辑，并保持现有接口不变。
```

常用字段：

| 字段 | 作用 |
| --- | --- |
| `repo` | 相对 `--repo` 的任务仓库 |
| `test_path` / `test_cmd` | 目标测试或自定义验证命令 |
| `lint_cmd` | lint grader |
| `patch_policy_cmd` | patch policy grader |
| `target_files` | 优先检索和分析的文件 |
| `exclude_paths` | repo-map 和图分析排除路径 |
| `max_steps` | 单任务最大步数 |
| `finish_if_verified` | 目标验证通过时允许提前结束 |
| `skip_preverified` | 初始验证已通过时跳过模型调用 |

多个 grader 会全部执行并聚合结果。每个任务先复制到独立 workspace，再运行 agent 和 grader，不污染原始 fixture。

### 批量运行

```bash
agent benchmark run --repo . --tasks-dir ./benchmark_tasks
agent benchmark run \
  --repo . \
  --tasks-dir ./benchmark_tasks \
  --task-glob "v2_*.txt" \
  --limit 15 \
  --mechanism-profile full \
  --task-timeout-seconds 300
```

重要选项：

- `--workspace-root`：独立 workspace 根目录
- `--task-timeout-seconds`：父进程硬超时
- `--skip-preverified / --no-skip-preverified`：是否跳过初始已通过任务
- `--mechanism-profile baseline|partial|full`：机制组合
- `--sandbox`：任务命令在 Docker 中运行

### 汇总和对比

```bash
agent benchmark summarize --dir ./logs/artifacts
agent benchmark summarize --dir ./logs/artifacts --only-agent-runs --json-output
agent benchmark summarize --dir ./logs/artifacts --markdown-out summary.md

agent benchmark compare \
  --left ./logs/baseline/artifacts \
  --right ./logs/current/artifacts \
  --markdown-out compare.md

agent benchmark ablation-report \
  --dir ./logs/artifacts \
  --markdown-out ablation.md
```

报告包含成功率、任务数、平均 steps/tokens/耗时、失败类型和阶段分布、每任务结果、patch 链接及 baseline 对比。

### Fixture 与 patch

```bash
agent benchmark reset-fixtures --repo .
agent benchmark patch-replay \
  --artifact-dir ./logs/artifacts/<run> \
  --repo ./benchmark_fixtures/run_demo
```

阶段性 SWE-bench Lite 结果见 [docs/benchmark_results_30.md](docs/benchmark_results_30.md)。

## 5. 工件、日志与回滚

运行日志默认位于 `logs/*.jsonl`：

```bash
agent log list
agent log show logs/<run>.jsonl
```

artifact 默认位于 `logs/artifacts/<run>/`：

| 文件 | 内容 |
| --- | --- |
| `events.json` / `events.jsonl` | action、observation、reflection、终止事件 |
| `result.json` | status、summary、failure taxonomy |
| `metrics.json` | 运行和能力指标 |
| `run_manifest.json` | 模型、代码/任务版本、环境、source/workspace repo |
| `retrievals.json` | repo-map、关键词、符号和图检索 |
| `patches.json` | patch、reverse patch、冲突和回滚轨迹 |
| `memory_hits.json` | 本次使用的长期记忆 |
| `final_diff.patch` | 最终 diff |
| `final_report.md` | 单次运行报告 |

失败 run 会记录：

- `failure_type`：如 verification、timeout、workspace、tool、LLM 错误
- `failure_stage`：preverify、agent loop、grading、artifact export 等阶段
- `failure_message`：可读错误信息

查看和回滚 patch：

```bash
agent patch latest --artifact-dir logs/artifacts/<run>
agent patch rollback --artifact-dir logs/artifacts/<run> --repo /path/to/project
```

## 6. 记忆与上下文

PatchFlow 使用两层上下文：

- 短期历史：当前 run/chat 的最近消息
- 上下文压缩：窗口溢出时保留旧行动、文件、失败轨迹和成功模式摘要
- 长记忆：跨 run 检索同 repo、category、failure type 的经验

本地记忆文件：

```text
logs/memory/run_memory.jsonl
logs/memory/experience_memory.jsonl
```

通过 `context.enable_compression`、`context.enable_long_memory` 和 `context.long_memory_limit` 配置；也可以用 CLI 开关临时关闭。

## 7. Docker 与安全

Docker 沙箱：

```bash
docker info
agent run --task "修复并测试" --sandbox
agent chat --sandbox
```

沙箱默认断网，将目标 repo 挂载到 `/workspace`，session 结束后自动清理。镜像可通过 `PATCHFLOW_SANDBOX_IMAGE` 覆盖。

命令安全分三层：

- 硬拦截：明显破坏系统的命令永不执行
- 只读命令：状态查看、搜索、测试等直接执行
- 危险写操作：使用 `--confirm` 时执行前询问

`--confirm` 适合人工运行；自动 benchmark 通常依靠独立 workspace 和 Docker 隔离。

## 8. GitHub Issue

```bash
export GITHUB_TOKEN=ghp_xxx
python -m entry.github_issue \
  --repo owner/repo \
  --issue 42 \
  --local-path /tmp/project
```

该入口读取 Issue、运行 agent，并可创建 pull request。Token 需要相应仓库权限。

## 9. 写任务的建议

任务描述至少包含：

- 问题发生在哪个模块或行为
- 当前现象与预期结果
- 应运行的目标测试
- 不允许改变的接口或目录

示例：

```text
src/parser.py 的 parse() 在空字符串输入时抛出 ValueError，预期返回 None。
保持 parse() 的公开签名不变，不修改 tests/。
修复后运行 tests/test_parser.py::test_empty。
```

复杂任务建议写入 `task.txt` 后使用 `--task-file`。

## 10. 常见问题

**模型没有响应**

运行 `python scripts/smoke_test.py`，再用 `--verbose` 检查配置、网络和后端错误。

**任务重复操作**

Agent 内置循环检测；也可以中断后缩小任务范围、提供目标测试和目标文件。

**token 消耗较高**

使用更快模型，降低 `repo_map_budget`、`history_window` 或 `max_steps`；benchmark 应优先配置精准 grader。

**沙箱缺依赖**

使用包含依赖的镜像并设置 `PATCHFLOW_SANDBOX_IMAGE`，或在任务允许时先安装依赖。默认沙箱断网。

**需要查看所有 CLI 选项**

```bash
agent --help
agent run --help
agent benchmark run --help
```

## 11. 开发验证

```bash
pip install -e ".[dev]"
python -m pytest -q
python scripts/smoke_test.py
```

涉及 benchmark/harness 时，至少运行：

```bash
python -m pytest tests/test_day1.py tests/test_day6.py tests/test_sandbox.py -q
```
