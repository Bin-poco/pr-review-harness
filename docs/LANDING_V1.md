# 落地第一阶段：可靠提交与 GitHub 接入

日期：2026-10-02。此记录对应本次改造，不覆盖此前封存的首轮实验。

## 本次实现

| 模块 | 行为 | 主要源码 |
| --- | --- | --- |
| 提交修复 | 识别无提交、截断、参数/位置错误；预算内限次修复，失败保留诊断 | `submission.py`、`runtime.py` |
| 输出诊断 | finish_reason、工具名称、无效参数摘要进入回执和报告 | `persistence.py` |
| GitHub 输入 | API 固定 SHA、获取提交、再次确认版本、稳定仓库身份 | `github.py`、`snapshot.py` |
| 发布 | 默认预览，显式 COMMENT；版本校验、行内位置和重复发布控制 | `github.py`、`cli.py` |
| 人工反馈 | `memory --pr` 使用同一 GitHub 仓库身份，复用已有生命周期管理 | `cli.py`、`memory.py` |

运行步骤见 [GitHub 接入指南](GITHUB_INTEGRATION.md)。

## 工程验证

- `uv run pytest -q --tb=short`：**150 项通过**，耗时 66.20 秒。
- `uv run ruff check src tests evaluation/submission_replay.py`、本次修改文件的格式检查、`git diff --check`：通过。
- 新增测试覆盖提交修复次数、预算耗尽、恢复累计、重复错误 ID；GitHub 预览、版本过期、发布去重、响应未知、分页、稳定身份和人工反馈入口。
- 预设模型测试验证机制，不代表真实模型审查质量。

## 原失败案例的真实模型重放

原跨仓库试跑共有 36 次审查，其中 3 次未完成有效提交。本次只重放这三个已知失败，保持原固定 SHA、模型与基础预算，启用新提交修复。结果另存，不改写原批次。

| 案例 / 变体 | 本次结果 | 模型 / 工具尝试 | 修复次数 | 本次观察 |
| --- | --- | --- | --- | --- |
| Werkzeug #3081 / working | 有效提交，1 条意见 | 5 / 6 | 1 | 再次截断后修复提交 |
| Werkzeug #3113 / working | 有效提交，1 条意见 | 5 / 7 | 1 | 再次截断后修复提交 |
| Click #2006 / baseline | 有效提交，1 条意见 | 10 / 18 | 0 | 原 JSON 错误没有复现，直接成功 |

本机原始记录：replay.json（本机 `outputs/submission-replay/44316a8dd93d4f75b072a36e4ab8e5ed/replay.json`）。重放入口为 `evaluation/submission_replay.py`；只读取旧失败及运行配置，不把 gold 交给模型。

这三个案例是已知故障诊断。两例直接观察到修复完成；第三例成功不能归因于修复机制。**3/3 完成不表示准确率提升，也不表示新增意见已经由独立人工确认。** 不与旧批次合并成新的成功率结论。

## 真实 GitHub 输入到报告

公开 PR [pallets/click#2818](https://github.com/pallets/click/pull/2818) 已完成获取和 DeepSeek 审查：

- base / merge base：`5961d31fb566f089ad468a5b26a32f1ebfa7f63e`。
- head：`a6ae8aaf1c24c9517585d117b493751b1bd8e867`。
- 5 次模型尝试、6 次工具尝试，提交修复 0 次，有效提交空意见。
- 本机审查报告（本机 `outputs/landing-v1/github-review/review.md`）和运行 JSON（本机 `outputs/landing-v1/github-review/review.json`）。

该 PR 已关闭，本次仅做读取和本地审查，没有发布。空意见不证明不存在缺陷。

## 真实测试发布与重复检查

经用户授权，在 [Bin-poco/PharosRAG](https://github.com/Bin-poco/PharosRAG) 创建 [Draft 测试 PR #4](https://github.com/Bin-poco/PharosRAG/pull/4)。仅新增 `examples/pr_review_harness_smoke.py`，没有接入应用入口；PR 保持 Draft，未合并。

文件是人工构造的边界样例：`mean_chunk_score` 约定空列表返回 0.0，却直接除以列表长度。真实模型生成一条 P2 意见，定位到新增的第 6 行；人工执行独立样例确认空列表抛出 ZeroDivisionError。模型自身只执行语法检查，语法通过不能证明行为正确。

- base / merge base：`26061d62fb66cc7c89619b3c8ee6a23f11f56c2e`。
- head：`2df0e0865ce824d34b3c878656f4cb83304febb0`。
- 4 次模型尝试、7 次工具尝试，修复 0 次，有效提交 1 条意见。
- [COMMENT 审查](https://github.com/Bin-poco/PharosRAG/pull/4#pullrequestreview-5393862448)与[第 6 行评论](https://github.com/Bin-poco/PharosRAG/pull/4#discussion_r4167390177)已发布。
- 原本地回执目录重跑返回 `already_published`；换空回执目录，通过远程标记查回同一审查，仍返回 `already_published`。
- 远程再次读取确认只有 1 条 COMMENTED 审查和 1 条行内评论，绑定上述 head。

本机证据：模型报告（本机 `outputs/landing-v1/pharos/review/review.md`）、预览（本机 `outputs/landing-v1/pharos/preview/publication.json`）、远程对账记录（本机 `outputs/landing-v1/pharos/remote-proof.json`）。

人工构造样例证明输入、审查、发布与重复检查链路，不计入公开 PR 质量评测。未把样例意见自动写成生产仓库的已确认记忆。

## 当前边界与下一阶段

已经具备手动输入、审查、预览、发布与人工反馈的代码链路。自动触发、队列、容器隔离、增量审查和部署运维尚未完成；不能称为企业生产系统。

下一步优先隔离代码执行，再接 GitHub Actions/Webhook 并增加并发、取消和失败恢复管理。独立人工根因复核、误报分析与历史 PR 到后续 PR 的记忆实验继续推进。

运行身份包含实现摘要。代码变更后旧 run 可能不能在当前版本恢复；应保留对应源码或建立新 run。旧 `FREEZE.json` 的当前源码校验失败是版本变更的预期结果，不修改原封存文件来掩盖变化。
