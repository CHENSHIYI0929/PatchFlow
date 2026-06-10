# Benchmark Fixtures

这个目录保存专门给 `agent benchmark run` 使用的未修复样本。

设计原则：

- `demo/` 目录保留为演示和手动试玩用，可以处于“已修复”状态
- `benchmark_fixtures/` 保留为 benchmark 初始态，默认包含故意留下的 bug
- `benchmark_tasks/*.txt` 默认指向这里的子目录，而不是 `demo/`

推荐用法：

```bash
agent benchmark run \
  --repo /Users/chenshiyi/Downloads/forge-agent-main \
  --tasks-dir /Users/chenshiyi/Downloads/forge-agent-main/benchmark_tasks
```

如果你想手动验证某个 fixture 的初始失败状态，可以进入对应目录单独运行 pytest。
