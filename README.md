# PatchFlow

PatchFlow 是一个可运行、可评测的 coding agent。它会探索代码库、检索上下文、生成结构化 patch、运行目标验证，并把过程导出为可回放工件。

核心能力：

- 支持 Claude、OpenAI、DeepSeek、Groq、Ollama 等后端
- `chat`、一次性 `run`、GitHub Issue 三种入口
- repo-map、图邻居、混合检索与失败分析
- edit plan、patch 自审、结束前验证和回滚
- 长记忆与上下文压缩
- Docker 沙箱和命令安全策略
- benchmark grader、独立 workspace、失败分类、manifest 和对比报告

## 快速开始

要求 Python 3.11+。

```bash
git clone <repo-url> PatchFlow
cd PatchFlow
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

export SILICONFLOW_API_KEY=sk-xxx
python scripts/smoke_test.py
```

默认模型配置位于 `config/default.yaml`。API Key 建议通过环境变量提供，不要写入仓库。

## 日常使用

对话模式：

```bash
cd /path/to/project
agent chat
agent chat --model deepseek-ai/DeepSeek-V4-Flash
agent chat --sandbox
```

一次性任务：

```bash
agent run --repo /path/to/project --task "修复 parser 空字符串异常并运行目标测试"
agent run --repo /path/to/project --task-file task.txt
agent run --task "只分析风险，不修改文件" --run-mode safe
agent run --task "生成 patch，确认后应用" --run-mode review --confirm
```

运行结束后会输出事件日志和 artifact 目录。详细命令见 [USAGE.md](USAGE.md)。

## Benchmark

批量运行任务：

```bash
agent benchmark run \
  --repo . \
  --tasks-dir ./benchmark_tasks \
  --task-glob "v2_*.txt" \
  --limit 15 \
  --mechanism-profile full \
  --task-timeout-seconds 300
```

汇总、对比和消融报告：

```bash
agent benchmark summarize --dir ./logs/artifacts --markdown-out summary.md
agent benchmark compare \
  --left ./logs/baseline/artifacts \
  --right ./logs/current/artifacts \
  --markdown-out compare.md
agent benchmark ablation-report --dir ./logs/artifacts --markdown-out ablation.md
```

任务文件支持 front matter：

```text
---
repo: benchmark_fixtures/run_demo
test_path: test_report.py
lint_cmd: python -m ruff check .
patch_policy_cmd: python scripts/check_patch_policy.py
target_files: report.py, scores.py
exclude_paths: logs, README.md
max_steps: 12
finish_if_verified: true
skip_preverified: true
---
修复成绩报告生成逻辑，并保持现有接口不变。
```

`test_path` / `test_cmd` 会构造 `CommandGrader`；配置多个检查时使用 `CompositeGrader`，执行全部 test、lint、patch policy 并聚合结果。

每个 benchmark task 都在独立 workspace 中运行，不污染源 fixture。运行结果包含 `failure_type`、`failure_stage`、`failure_message`，并写入 `run_manifest.json`。

阶段性 SWE-bench Lite 结果见 [docs/benchmark_results_30.md](docs/benchmark_results_30.md)。

## 运行工件

每次 run 通常生成：

| 文件 | 内容 |
| --- | --- |
| `events.json` / `events.jsonl` | 完整事件流 |
| `result.json` | 最终状态和失败分类 |
| `metrics.json` | steps、tokens、检索、patch、grader 等指标 |
| `run_manifest.json` | 模型、代码版本、任务版本、环境和 workspace |
| `retrievals.json` | repo-map、搜索和图检索轨迹 |
| `patches.json` | patch、reverse patch、冲突和回滚记录 |
| `memory_hits.json` | 本次注入的长期记忆 |
| `final_diff.patch` | 最终 diff（有修改时） |
| `final_report.md` | 单次运行摘要 |

常用操作：

```bash
agent log list
agent log show logs/<run>.jsonl
agent patch latest --artifact-dir logs/artifacts/<run>
agent patch rollback --artifact-dir logs/artifacts/<run> --repo /path/to/project
```

## 配置

最小配置：

```yaml
llm:
  provider: openai
  model: deepseek-ai/DeepSeek-V4-Flash
  api_key: ${SILICONFLOW_API_KEY}
  base_url: https://api.siliconflow.cn/v1

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

CLI 的 `--provider`、`--model`、`--max-steps` 会覆盖配置文件。

## 安全与执行模式

- `safe`：只分析和计划，不写文件
- `review`：生成 patch preview，确认后应用
- `auto`：自动修改和验证，默认模式
- `--confirm`：危险 shell 命令执行前确认
- `--sandbox`：在默认断网的 Docker 容器内运行命令和测试

benchmark 还支持 `baseline`、`partial`、`full` 三种机制 profile，以及单独关闭 failure analyzer、hybrid retrieval、edit plan、self-review、long memory 和 compression。

## 开发

```bash
pip install -e ".[dev]"
python -m pytest -q
python scripts/smoke_test.py
```

主要目录：

```text
agent/              Agent loop、grader、failure、artifact、benchmark
context/            repo-map、历史、token 预算、压缩
tools/              文件、搜索、测试、git、shell、runtime
llm/                模型后端和路由
entry/              CLI、chat、GitHub Issue 入口
benchmark_tasks/    benchmark task manifests
benchmark_fixtures/ benchmark 初始仓库
tests/              自动化测试
```

完整教程见 [USAGE.md](USAGE.md)。
