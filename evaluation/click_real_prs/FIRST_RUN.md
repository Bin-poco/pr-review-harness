# 首轮真实模型评测记录（2026-10-02）

本轮固定了 3 个 Click PR 的 merge base/head SHA，使用 DeepSeek 官方 `deepseek-flash`、关闭思考模式、温度 0，执行 `baseline`（无工作状态）和 `working`（有工作状态）各一次。每次主审查最多 12 次模型调用、24 次工具调用，读取上限 40,000 字符。完整轨迹和用量保存在本机 `outputs/click-real-prs/c61bc4faee144e41be4186206da976c3/`；密钥不进入报告。所有案例均为公开开发集，结果不能当作封存测试集或稳定性能估计。

## 结果

| PR | Baseline | Working |
| --- | --- | --- |
| [#2934](https://github.com/pallets/click/pull/2934) 迭代标准输入 | 找到已知根因 | 找到已知根因 |
| [#1572](https://github.com/pallets/click/pull/1572) 分页输出 | 漏掉已知的 stdout 关闭问题；找到另一个经复现的无标准输出回归；另有一条兼容性意见待维护者判定 | 找到 stdout 关闭问题；漏掉无标准输出回归；另有一条关于临时文件的误报 |
| [#2940](https://github.com/pallets/click/pull/2940) 修复迭代问题 | 未报告目标缺陷 | 未生成有效结论：在预算结束前仍反复请求不允许执行的测试 |

第一版标签只有两个历史问题。审查后，模型提出 #1572 的 `StringIO` 回退路径问题；独立脚本验证 base 正常、head 抛 `TypeError`，于是将其加入**第二版标签**。`reproduce.py` 还验证：自定义非终端流带有 `color` 属性时，base 会剥除 ANSI、head 会保留，但这是否违反维护者预期仍未确定；临时文件路径在 base/head 都正常，模型关于“提前关闭”的论证与实际上下文退出顺序不符。

用第二版标签重新计算旧报告的位置匹配，baseline 在两个有问题 PR 上是 2 个位置命中、1 个目标遗漏；working 是 3 个位置命中、0 个目标遗漏。但 working 的第三个位置命中属于**错误根因**：它指向临时文件关闭，恰好与新标签的 `StringIO` 行号重叠。本助手逐条对照源码和复现结果后，两组均是 2 个确认发现、1 个确认遗漏；working 还有 1 条确认误报，baseline 有 1 条尚不能定性的兼容性意见。这是开发阶段的复核，尚无独立人类审查者确认。working 在修复 PR 上执行失败，不能用完成样本的分数宣称总体提升。

## 复核和重算

在项目根目录运行：

```bash
uv run python evaluation/click_real_prs/reproduce.py
uv run python evaluation/click_real_prs/rescore.py \
  outputs/click-real-prs/c61bc4faee144e41be4186206da976c3/benchmark.json
```

第一条命令验证固定 SHA 并执行 base/head 复现；第二条不重新调用模型，只用当前标签重算保存的报告，并保留原始 `benchmark.json`。本助手的逐条判断见[可追溯复核记录](assistant_adjudication.json)，本机批次中也有 `human-review.json`；后者名称来自工具模板，不能理解为独立人工审查。标签来自公开后续 issue、修复 PR，以及审查后新增的可复现候选；因此这批数据只能用于开发和排错。

首轮之后，`run_check` 工具参数在未启用测试时改为仅允许 `syntax`。仅针对 #2940 的 working 组做了一次修复后冒烟运行，记录在 `outputs/click-2940-check-schema/8250bf488600447882846e0be859aa0a/`：这次正常完成，提交空发现。它属于**改动后的单独试运行**，不能混入首轮对照或用来证明工作状态的收益。下一步应增加从未用于调参的其他仓库 PR、重复运行，并请独立人类审查者复核争议意见。
