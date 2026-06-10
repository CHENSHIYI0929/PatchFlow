# Flash Demo: 小范围 Bug 修复

这个 demo 专门给 `deepseek-v4-flash` 这样的快速模型使用。

它模拟的场景是：
- 一个很小的 Python 文件
- 一个明确的 bug
- 一组很快就能跑完的测试

## 为什么这个场景适合 flash

- 任务简单，几乎不需要长链推理
- 代码量很小，repo-map 和上下文开销低
- 改动目标单一，适合 `apply_patch`
- 测试反馈短，容易快速迭代

## 运行方式

```bash
cd /Users/chenshiyi/Downloads/forge-agent-main/demo/flash_demo
agent run --repo . --task-file task.txt --model deepseek-v4-flash
```

如果你希望更安全一点：

```bash
agent run --repo . --task-file task.txt --model deepseek-v4-flash --sandbox
```

## 预期结果

运行后，agent 应该修复 `buggy_math.py` 里 `safe_divide()` 在除数为 0 时抛异常的问题，
并保持正常除法行为不变。

