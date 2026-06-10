# Run Demo

这个 demo 专门给 `agent run` 设计，适合展示“一次性给定任务，然后自动跑完”的使用方式。

特点：

- 任务写在 `task.txt`
- 代码分散在多个小文件里
- 需要 agent 自己读代码、找问题、修测试
- 仍然足够小，运行时间不会太长

## 运行方式

```bash
cd /Users/chenshiyi/Downloads/forge-agent-main/demo/run_demo
agent run --repo . --task-file task.txt --model deepseek-ai/DeepSeek-V4-Flash
```

如果你希望更安全一点：

```bash
agent run --repo . --task-file task.txt --model deepseek-ai/DeepSeek-V4-Flash --sandbox
```

## 预期结果

运行后，agent 应该修复 `report.py` 和 `scores.py` 里的问题，让 `test_report.py` 全部通过。

