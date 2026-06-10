# Flash Plus Demo

这个 demo 比 `flash_demo/` 稍复杂一点，适合用来观察 `deepseek-ai/DeepSeek-V4-Flash`
在小型多文件修复任务上的表现。

它的特点是：

- 需要读 2 到 3 个源码文件
- 有多个测试断言，不只是一个单点 bug
- 涉及一点点业务规则理解
- 仍然能在很短时间内验证

## 场景

这里模拟一个很小的结算模块：

- `discounts.py` 负责优惠券解析
- `tax.py` 负责税率查询
- `checkout.py` 负责最终结算

当前有两个问题：

- 小写或带空格的优惠券不会被正确识别
- 税额是在折扣前计算的，和测试预期不一致

## 运行方式

```bash
cd /Users/chenshiyi/Downloads/forge-agent-main/demo/flash_plus_demo
agent run --repo . --task-file task.txt --model deepseek-ai/DeepSeek-V4-Flash
```

更安全的方式：

```bash
agent run --repo . --task-file task.txt --model deepseek-ai/DeepSeek-V4-Flash --sandbox
```

