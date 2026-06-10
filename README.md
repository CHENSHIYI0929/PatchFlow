# Forge Agent

自主编程智能体。给它一个任务描述，它会自己探索代码库、检索上下文、优先用结构化 patch 修改文件、运行测试，直到完成。

它不是单纯的“会改文件”工具，而是一个可评测 coding agent 平台，已经把任务 manifest、benchmark 分层、图结构代码理解、patch 回滚 / 冲突 / 重放，以及运行工件和统计分析都接了起来。

支持 **Claude、DeepSeek、OpenAI、Groq、Ollama** 多种模型，内置流式输出、Docker 沙箱、GitHub Issue 自动修复。

---

## 快速开始

```bash
# 安装
git clone <repo-url> && cd PatchFlow
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 配置（编辑 config/default.yaml，填入 provider 和 api_key）
export SILICONFLOW_API_KEY=sk-xxx   # 或 ANTHROPIC_API_KEY / OPENAI_API_KEY

# 验证
python scripts/smoke_test.py

# 使用
cd your-project
agent chat
```

运行 `agent run ...` 后，终端会同时打印：
- `Log: ...jsonl`：完整事件日志
- `Artifacts: .../logs/artifacts/<task_id_timestamp>`：结构化运行工件目录

如果你想先感受一下 `deepseek-ai/DeepSeek-V4-Flash` 的适用场景，可以直接看 `demo/flash_demo/`：
- 单文件、小范围 bug 修复
- 测试快、反馈短
- 很适合用 `apply_patch` 做最小修改

如果你想快速看“图结构理解有没有生效”，最短路径是：
```bash
agent benchmark run --repo . --tasks-dir ./benchmark_tasks --task-glob "03_run_demo.txt" --limit 1
agent benchmark summarize --dir ./logs/artifacts --only-agent-runs --json-output
```
然后打开对应 `logs/artifacts/<run_id>/retrievals.json`，看是否出现了 `graph_neighbors`、`file_read`、`auto_graph_probe`、`auto_file_prefetch`。

---

## 使用方式

### chat 模式（推荐）

持续对话，每轮历史保留，最接近 Claude Code 的体验：

```bash
agent chat                            # 当前目录
agent chat --repo /path/to/project   # 指定目录
agent chat --model deepseek-ai/DeepSeek-V4-Flash   # 切换模型
agent chat --sandbox                  # Docker 沙箱
```

对话内命令：`/exit` 退出、`/stats` 查看统计、`/clear` 清空历史、`/help` 帮助

### run 模式

一次性任务，适合明确的批处理场景：

```bash
agent run --task "修复所有 failing 的测试"
agent run --task-file task.txt           # 从文件读任务
agent run --task "..." --confirm         # 危险命令需确认
agent run --task "..." --sandbox         # Docker 沙箱
```

`run` 模式会自动导出分析工件，包含：
- `events.json`：完整事件序列
- `metrics.json`：基础指标（成功率、tool calls、retrieval、graph queries、patch）
- `run_manifest.json`：模型配置、代码版本、任务版本、运行环境与 workspace 元数据
- `retrievals.json`：repo-map、搜索命中和 `graph_neighbors` 查询
- `patches.json`：每次 `apply_patch` / `revert_patch` / `file_write` 的结构化记录，含 reverse patch 和冲突信息
- `final_diff.patch`：最终 git diff（如有）

你也可以直接汇总这些工件：

```bash
agent benchmark summarize --dir ./logs/artifacts
agent benchmark summarize --dir ./logs/artifacts --only-agent-runs
agent benchmark summarize --dir ./logs/artifacts --markdown-out ./summary.md
agent benchmark compare --left ./logs/baseline/artifacts --right ./logs/new/artifacts
agent benchmark compare --left ./logs/baseline/artifacts --right ./logs/new/artifacts --only-agent-runs
agent benchmark compare --left ./logs/baseline/artifacts --right ./logs/new/artifacts --markdown-out ./compare.md
agent benchmark run --repo . --tasks-dir ./benchmark_tasks
agent benchmark run --repo . --tasks-dir ./benchmark_tasks --task-glob "*.txt" --limit 2
agent benchmark patch-replay --artifact-dir ./logs/artifacts/<run_id> --repo ./benchmark_fixtures/run_demo
agent benchmark reset-fixtures --repo .
```

`benchmark run` 现在支持任务文件 front matter。每个任务都可以声明自己的 `repo`、`test_path`、`test_cmd`、`lint_cmd`、`patch_policy_cmd`、`exclude_paths`、`target_files`、`max_steps` 和 `finish_if_verified`。`test_cmd` 会被封装成 `CommandGrader`；如果同时声明了 `lint_cmd` 或 `patch_policy_cmd`，会自动组合成 `CompositeGrader`，按顺序执行 test / lint / patch policy。
每个 benchmark task 每次运行都会先复制到独立 workspace，再在 workspace 里跑 agent 和 grader，不会污染原始 fixture。`run_manifest.json` 会记录 `repo_source`、`workspace_repo`、模型配置、代码版本、任务文件 hash 与运行环境，便于复现和对比。
默认还会启用 `--skip-preverified`：如果目标验证在任务 workspace 里已经通过，就直接产出 `tokens=0 / steps=0` 的 benchmark 工件并跳过模型调用。
如果你只想看真正调了模型的样本，可以对 `summarize/compare` 加 `--only-agent-runs`。如果 benchmark 把 `benchmark_fixtures/` 改脏了，可以用 `benchmark reset-fixtures` 从基线快照恢复。
benchmark 汇总里还会额外统计 `graph_queries`、`patch_conflicts`、`patch_reverts`、`failure_type_distribution` 和 `failure_stage_distribution`。每次 run 除了 success 之外，也会导出 `failure_type`、`failure_stage` 和 `failure_message`，方便区分是 agent、grading、workspace 还是模型层出的问题。
Markdown benchmark report 现在会输出固定模板：总成功率、任务数、平均 steps、平均 tokens、平均耗时、失败类型分布、每任务结果、patch 文件名，以及和 baseline 的对比。
如果你想把某次结构化 patch 或它的 reverse patch 重放到仓库里，可以使用 `benchmark patch-replay`。

### GitHub Issue 自动修复

```bash
export GITHUB_TOKEN=ghp_xxx
python -m entry.github_issue \
    --repo owner/repo --issue 42 --local-path /tmp/myrepo
```

自动拉取 Issue → 运行 agent → 提交 PR。

---

## 配置

编辑 `config/default.yaml`：

```yaml
llm:
  provider: deepseek                      # anthropic | openai | deepseek | groq | ollama
  model: deepseek-ai/DeepSeek-V4-Flash
  api_key: ${SILICONFLOW_API_KEY}         # 从环境变量读取
  base_url: https://api.siliconflow.cn/v1 # OpenAI-compatible 时填写，anthropic 留空

agent:
  max_steps: 40           # 每轮最大步数
  budget_tokens: 80000    # token 预算

context:
  repo_map_budget: 8000   # repo-map 注入量
  history_window: 20      # 保留历史轮数
```

---

## 项目结构

```
PatchFlow/
├── agent/              # 核心：ReAct 主循环、事件日志、数据结构
│   ├── core.py         # Agent 类，驱动整个运行循环
│   ├── task.py         # Task / Action / Observation / RunResult 数据类
│   ├── patch.py        # 结构化 Patch 对象（replace_file / search_replace / replace_range）
│   ├── event_log.py    # JSONL append-only 事件流，支持回放
│   ├── artifacts.py    # 运行工件导出（events / metrics / retrievals / patches）
│   └── prompt.py       # System prompt 模板
│
├── llm/                # LLM 后端
│   ├── base.py         # LLMBackend 抽象基类，含默认 stream()
│   ├── anthropic_backend.py   # Claude 原生（tool_use + 流式）
│   ├── openai_compat.py       # OpenAI / DeepSeek / Groq / Ollama
│   └── router.py       # 按配置选择 backend
│
├── tools/              # 工具层（agent 可调用的操作）
│   ├── base.py         # BaseTool + ToolRegistry
│   ├── file_tool.py    # 文件读写查看 + apply_patch / revert_patch
│   ├── shell_tool.py   # Shell 执行（四层安全防护）
│   ├── search_tool.py  # 文本搜索 / 文件查找 / 符号定位 / graph_neighbors
│   ├── test_tool.py    # pytest 执行 + 结构化结果解析
│   ├── git_tool.py     # git status / diff / add / commit
│   └── runtime.py      # LocalRuntime / DockerRuntime
│
├── context/            # 上下文管理
│   ├── repo_map.py     # tree-sitter 多语言符号提取，生成 repo 摘要
│   ├── token_budget.py # Token 预算分配与裁剪
│   └── history.py      # 对话历史滑动窗口
│
├── entry/              # 入口层
│   ├── cli.py          # Click CLI（run / chat / log 子命令）
│   ├── chat.py         # ChatSession，跨轮持久化历史
│   └── github_issue.py # GitHub Issue → PR 自动化
│
├── config/
│   ├── default.yaml    # 默认配置
│   └── schema.py       # 配置加载与校验
│
├── tests/              # 测试覆盖核心模块
├── scripts/
│   └── smoke_test.py   # 端到端联通验证
├── examples/
│   ├── quicksort.py    # 独立算法示例
│   └── linked_list.py  # 独立数据结构示例
└── USAGE.md            # 完整使用教程
```

---

## 核心特性

**多模型支持**
- Anthropic Claude（原生 tool_use）
- OpenAI、DeepSeek、Groq、Ollama（OpenAI-compatible）
- DeepSeek R1 等不支持 function calling 的模型走文本解析 fallback
- 配置文件一行切换，或 `--model` 参数临时覆盖

**多语言 Repo-map + 可追踪文件块**
用 tree-sitter 精确提取符号（函数、类、方法），生成 repo 摘要注入 system prompt，
同时记录被纳入上下文的结构化 trace chunks，便于离线分析和 benchmark。
测试失败后会自动结合图关系排序候选文件，优先 `file_read` 显式 `target_files`，再用 `graph_neighbors` 探测相关模块，把结果写回上下文。

**流式输出**
模型 thought 逐 token 实时打印，工具调用实时显示，体验接近 Claude Code。

**安全机制（三层）**
- 硬拦截黑名单：`rm -rf /`、`mkfs` 等永不执行
- 只读白名单：`ls`、`grep`、`git status`、`pytest` 等直接执行
- 写操作确认：`--confirm` 模式下 `git commit`、`pip install` 等需 y/n 确认

**Docker 沙箱**
`--sandbox` 参数，所有命令在 `python:3.11-slim` 容器里执行，
repo 通过 bind mount 双向同步，默认断网。

**Reflection 机制**
- 测试失败 → 自动触发反思 prompt，重新分析错误原因
- 连续 6 步无文件修改 → 触发反思，防止探索死循环
- 连续 3 步相同操作 → 判定死循环，自动终止

**Patch-first 编辑**
- 优先使用 `apply_patch` 做结构化编辑，而不是直接整文件 `file_write`
- 当前支持 `replace_file`、`search_replace`、`replace_range`
- 每次 patch 都会记录 `patch_id`、参数和执行结果，便于回滚和 benchmark 统计
- 支持冲突前置校验（期望内容 / 行范围 / 替换命中次数）
- 成功应用后会返回 `reverse_patch`，可直接交给 `revert_patch` 做回滚

**事件日志与运行工件**
- 每次运行生成 JSONL 日志，记录 action / observation / reflection / repo-map
- step 级别记录 `step_id`、`action_id`、`tool_call_id`
- 自动导出 `events.json`、`metrics.json`、`retrievals.json`、`patches.json`
- `patches.json` 记录 `reverse_patch`、冲突和回滚信息，便于 `benchmark patch-replay`
- 支持完整回放和基础 benchmark 指标分析

---

## 安全说明

`--confirm` 模式（`run`）和 `chat` 模式默认对写操作要求确认，执行前显示：

```
  ⚠  Agent wants to run:
     $ git commit -m "fix parser bug"
  Allow? [y/N]
```

`--sandbox` 模式在 Docker 容器中执行，宿主机环境完全隔离。

---

## 开发

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 运行测试
pytest                     # 全量
pytest tests/test_day3.py  # 单个文件

# 可选：更多语言的 tree-sitter 支持
pip install tree-sitter-javascript tree-sitter-typescript \
            tree-sitter-go tree-sitter-rust tree-sitter-java

# 可选：精确 token 计数
pip install tiktoken

# 可选：启用 Anthropic / GitHub Issue 相关测试
pip install anthropic PyGithub
```

---

## 命令参考

```bash
# chat
agent chat [--repo PATH] [--model MODEL] [--sandbox] [-v]

# run
agent run --task TEXT [--repo PATH] [--task-file FILE]
          [--model MODEL] [--confirm] [--sandbox] [--no-stream] [-v]

# log
agent log list [--dir DIR]
agent log show LOG_FILE

# github issue
python -m entry.github_issue \
    -r owner/repo -i ISSUE_NUM -l LOCAL_PATH [--no-pr] [-v]
```

详细用法见 [USAGE.md](USAGE.md)。
