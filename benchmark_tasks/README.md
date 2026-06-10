# Benchmark Tasks

这个目录提供一组可直接用于 `agent benchmark run` 的任务文件。

推荐用法：

```bash
agent benchmark run \
  --repo /Users/chenshiyi/Downloads/forge-agent-main \
  --tasks-dir /Users/chenshiyi/Downloads/forge-agent-main/benchmark_tasks
agent benchmark summarize --dir /Users/chenshiyi/Downloads/forge-agent-main/logs/artifacts --only-agent-runs
agent benchmark reset-fixtures --repo /Users/chenshiyi/Downloads/forge-agent-main
```

注意：

- 这些任务文件现在带有 front matter，会自动声明各自的 `repo`、`test_path` / `test_cmd`，以及可选的 `lint_cmd`、`patch_policy_cmd`、`exclude_paths`、`target_files`
- `agent benchmark run` 会根据 front matter 把任务缩到对应的 `benchmark_fixtures/` 子目录，并先复制到独立 workspace 再执行
- `finish_if_verified: true` 会允许“目标测试已通过且本轮无编辑”时提前结束，降低已修复样本的 token 成本
- `skip_preverified: true` 会在进入 LLM 前先做目标测试预检
- 如果任务声明了 `target_files`，失败后会优先读取这些目标文件，并自动探测它们的图邻居
- 每次 run 的 artifact 会多出一份 `run_manifest.json`，记录模型配置、任务文件 hash、代码版本、source repo 和 workspace repo
- `demo/` 目录用于演示；真正的 benchmark 初始态在 `benchmark_fixtures/`

任务文件示例：

```text
---
repo: benchmark_fixtures/run_demo
test_path: test_report.py
exclude_paths: README.md, logs
target_files: report.py, scores.py
finish_if_verified: true
skip_preverified: true
max_steps: 12
---
修复 `benchmark_fixtures/run_demo` 中的成绩报告生成逻辑。
```
