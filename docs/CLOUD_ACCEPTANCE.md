# 云端验收：GitHub Actions 预览

日期：2026-10-03，Asia/Shanghai。此次验收使用本仓库的人工构造样例，验证云端工程链路，不计入审查质量评测。

## 1. 实际运行

| 项目 | 记录 |
| --- | --- |
| 测试 PR | [Draft PR #1](https://github.com/Bin-poco/pr-review-harness/pull/1)，仅新增独立演示文件，保持未合并 |
| 手动云端预览 | [Actions 37085323726](https://github.com/Bin-poco/pr-review-harness/actions/runs/37085323726)，workflow_dispatch，success |
| 自动 Draft 事件 | [Actions 37085297132](https://github.com/Bin-poco/pr-review-harness/actions/runs/37085297132)，pull_request_target，正确跳过 Draft |
| Harness 源码版本 | `ac13e55d4127b36a35e66464933d6734c87d2541` |
| 被审查 base / merge base | `ac13e55d4127b36a35e66464933d6734c87d2541` |
| 被审查 head | `d321aad779953819c92bc3b2ce4f554b0de357f1` |
| 模型 | DeepSeek 官方 `deepseek-flash`，温度 0，关闭思考，报告 mode=live |
| 审查 run ID | `e3f48c263a514dfd8a48d180d6acb567` |
| 发布模式 | preview；远程审查、行内评论、普通评论均为 0 |

结构化验收摘要、原 artifact SHA-256 和运行链接保存于 [cloud-preview-20261003.json](validation/cloud-preview-20261003.json)。没有改写此前的本机验收、评测结果或 FREEZE。

## 2. 核对结果

手动运行完成可信源码检出、锁定依赖安装、固定 PR 输入、真实模型审查、独立核验、发布预览和 artifact 上传。review job 从 09:14:29 到 09:14:53，约 24 秒；这是单次人工样例的执行时长，不是性能基准。

- 模型提交 1 条 P2：`examples/cloud_preview_smoke.py:6` 的空列表除零问题。文档约定空输入返回 0.0，而实现直接计算 `sum(scores) / len(scores)`。
- 独立核验状态 completed，对该意见给出 supported；核验仍是模型判断。
- 累计 6 次模型尝试、9 次工具尝试；报告已知用量 20,130 tokens，unknown_usage_calls=0。提供方内部重试可能未暴露，不能据此保证精确账单。
- 结构化提交修复 0 次，提交 outcome=submitted。
- `publication.json` 为 preview，`automation.json` 为 completed；另查 GitHub 确认没有发送审查或评论。
- 只启用代码读取与语法检查，没有执行 PR 测试或安装脚本。
- 下载的 artifact 和运行日志检查未发现本机模型密钥或常见凭据格式；完整产物留在被忽略的本机 `outputs/cloud-acceptance/`。

自动 PR 事件的 artifact 仅包含 `automation.json`，状态 skipped、原因为 closed_or_draft。它验证了 Draft 跳过路径，没有调用模型；运行日志也确认实际检出的是可信默认分支的 `ac13e55`。本次未覆盖非 Draft 自动审查；该路径后来在 [PharosRAG 专项验收](PHAROS_CLOUD_ACCEPTANCE.md)中完成。

## 3. 如何学习这次运行

1. 在 Actions 页面看可信源码、依赖安装、Review event 与 Save review 步骤。
2. 下载手动运行 artifact，核对 `review.json` 的版本、mode、findings、trace、budget_usage、verification、submission。
3. 比较 `publication.json` 的预览 payload 与 PR 上的 0 条远程审查，解释“生成意见”和“发送意见”的边界。
4. 对照 Draft 事件的 automation.json，回到 `automation.py::parse_event` 找到跳过条件。

工作流 artifact 保留 7 天，公开摘要保存当次观察和文件摘要。后续文档提交会推进默认分支；以上 SHA 是当次输入。复跑需重新获取版本，旧报告不能直接用于已变化的 PR base/head。

## 4. 已完成和下一步

已完成本仓库云端手动预览和自动 Draft 跳过验收。`HARNESS_ENABLED=true`，`HARNESS_PUBLISH=false`，自动发布仍未开启。

本次验收结束时，PharosRAG 尚未部署调用方工作流。同日后续已完成[业务仓库部署与非 Draft 自动预览](PHAROS_CLOUD_ACCEPTANCE.md)，记录单独保存。跨 job 记忆/checkpoint、增量审查、队列和审查质量改善仍待完成。
