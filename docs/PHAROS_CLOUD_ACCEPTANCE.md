# 业务仓库验收：PharosRAG 自动审查预览

日期：2026-10-03，Asia/Shanghai。继[本仓库云端手动预览](CLOUD_ACCEPTANCE.md)后，本次把固定版本的 Harness 部署到 PharosRAG，并用真实 PR 事件完成自动审查。样例是人工构造的独立文件，不计入质量评测。

## 1. 部署与运行证据

| 项目 | 记录 |
| --- | --- |
| 业务仓库部署 | [提交 40136ce](https://github.com/Bin-poco/PharosRAG/commit/40136cea7cac09b3b9306f87ce9a3b69b1a2410f)，只新增 `.github/workflows/pr-review.yml` |
| 固定 Harness 源码 | `Bin-poco/pr-review-harness@ac13e55d4127b36a35e66464933d6734c87d2541`，与前一阶段验收代码一致 |
| 测试 PR | [PharosRAG #5](https://github.com/Bin-poco/PharosRAG/pull/5)，仅新增 `examples/harness_auto_preview.py`，验收后恢复 Draft，未合并 |
| Draft 自动跳过 | [Actions 37086731665](https://github.com/Bin-poco/PharosRAG/actions/runs/37086731665)，success，`closed_or_draft` |
| 非 Draft 自动预览 | [Actions 37086749083](https://github.com/Bin-poco/PharosRAG/actions/runs/37086749083)，`pull_request_target`，success |
| 触发过程 | PR 在 09:36:37 转为 ready，09:36:39 自动创建运行；未使用手动 dispatch |
| 被审查 base / merge base | `40136cea7cac09b3b9306f87ce9a3b69b1a2410f` |
| 被审查 head | `80eb69da95f03d3e84a2b3905b887180269110ed` |
| 模型 | DeepSeek 官方 `deepseek-flash`，温度 0，关闭思考，报告 `mode=live` |
| 审查 run ID | `07a6f966d40742ce979788d97ce68abf` |
| 发布结果 | `preview`；审查、行内评论、普通评论均为 0 |

模型 Secret 由用户配置在业务仓库。仓库变量为 `HARNESS_ENABLED=true`、`HARNESS_PUBLISH=false`，模型和接口分别为 `deepseek-flash`、`https://api.deepseek.com`。上一阶段的 Draft PR #4 保持未合并。

## 2. 验收了什么

1. **运行的是固定 Harness 源码。** 下载日志确认 checkout 的仓库为 `Bin-poco/pr-review-harness`，实际 HEAD 为 `ac13e55`。运行页面显示的 PR head 不能替代这个核对。
2. **审查目标仍是 PharosRAG。** 报告 `source.repository_id=1367008779`、`source.number=5`，base/head 与 PR 一致。安装生成器只替换源码 checkout，不替换业务事件及 `GITHUB_REPOSITORY`。
3. **真实模型完成了审查和核验。** 发现 1 条 P2，位于 `examples/harness_auto_preview.py:6`：函数约定空列表返回 0.0，但实现除以 `len(scores)`，空列表会除零。独立核验完成并给出 supported，仍属于模型判断。
4. **预览产物完整且没有发送评论。** `review.md`、`review.json`、`publication.json`、`automation.json` 均已下载并核对；随后检查 GitHub，三个评论计数均为 0。
5. **预算和诊断有记录。** 6 次模型尝试、9 次工具尝试，已知用量 21,765 tokens，unknown_usage_calls=0，结构化提交修复 0 次。核验器有一次把版本参数写为 `merge-base`，收到工具错误后改为 `base`，在原预算内完成。

review job 从 09:36:42 到 09:37:11，约 29 秒。这是单个小型人工样例的观察值，不是性能基准。Harness 仅读取代码、做语法检查，没有执行 PR 测试或安装脚本；PharosRAG 原有 CI 是另一条独立工作流。

公开摘要与原始 artifact 的 SHA-256 保存于 [pharos-auto-preview-20261003.json](validation/pharos-auto-preview-20261003.json)。完整产物和日志保存在本机被忽略的 `outputs/pharos-cloud-acceptance/`，检查未发现本机模型密钥或常见凭据格式。GitHub artifact 保留 7 天，因此另存这份长期记录。旧验收与评测封存文件未改写。

## 3. 接下来怎样使用

- 在 PharosRAG 新建或更新非 Draft PR，或把 Draft 标记为 ready，会触发已启用的工作流。
- 在该仓库 Actions 中打开 **PR review harness**，下载对应 artifact，阅读 `review.md`，用 `review.json` 核对证据、版本、预算和核验状态。
- Draft 自动事件会跳过；需要检查 Draft 时，可在 PharosRAG 的该工作流手动填写 PR 编号。
- 目前仍为预览模式。不要把 `HARNESS_PUBLISH` 改成 `true`，除非已决定允许后续自动发送审查意见。
- 暂停模型调用可把 PharosRAG 的 `HARNESS_ENABLED` 改为 `false`。升级 Harness 需重新审核源码并更新工作流的完整 SHA；Harness 主分支推进不会自动改变业务仓库部署。

## 4. 学习与面试

对照业务工作流 → `automation.py::parse_event` → 固定版本获取与 `validate_source` → 审查和独立核验 → 发布预览，追踪这一次运行。重点解释：为什么执行源码的仓库与审查目标的仓库可以不同，为什么 Draft 跳过不等于完成了模型审查，为什么成功运行不能证明审查准确率。

现在可据实描述：**实现基于 GitHub PR 事件的自动审查，在独立业务仓库完成固定源码版本部署、真实模型审查、独立核验和报告归档，默认预览并校验输入版本。**

后续工程重点是跨 job 的人工反馈记忆与恢复状态存储、增量审查和调度；质量改善继续依靠人工根因复核与固定预算实验。本次没有验证自动发布、fork PR 路径、跨 job 持久化或记忆带来的质量收益。
