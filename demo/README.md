# Flash Demo

这个目录用来演示 `deepseek-v4-flash` 这类快速模型最适合的场景：

- 任务边界清晰
- 改动范围小，通常只动 1 个文件
- 测试快，能快速验证结果
- 不需要很长的上下文推理

当前包含两个 demo：

- `flash_demo/`：最小单文件 bug 修复
- `flash_plus_demo/`：稍复杂一些的多文件业务逻辑修复
- `run_demo/`：适合 `agent run` 的一次性自动修复任务

推荐用法：

```bash
cd /Users/chenshiyi/Downloads/forge-agent-main/demo/flash_demo
agent run --repo . --task-file task.txt --model deepseek-v4-flash
```

如果你想更稳一点，也可以加上 `--sandbox`。
