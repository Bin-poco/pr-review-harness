# 云端跨 job 语法检查缓存

## 1. 保存什么

每个 Actions job 的本机目录会消失。开启 `HARNESS_SYNTAX_CACHE_ENABLED=true` 后，工作流保存 `harness-syntax-运行编号-尝试次数` artifact；下次新审查在同一工作流最近 10 个已完成运行中寻找可用来源。

缓存只包含 Python `compile()` 的 passed/failed 结果、输入身份及原检查 run ID/SHA。它不保存旧模型意见、调度基线、单元测试结果或人工反馈。每次新审查仍调用模型并检查当前整个 PR；模型是否调用语法检查由本轮工具选择决定。

| 能力 | 复用内容 | 新审查的行为 |
| --- | --- | --- |
| 跨 job 语法缓存 | 确定性的编译结果 | 先读取当前固定 Git 对象，再按内容与编译器匹配 |
| [人工反馈记忆](CLOUD_MEMORY.md) | 维护者确认的规则 | 冻结当前有效反馈，供模型参考 |
| [checkpoint 恢复](CLOUD_RECOVERY.md) | 当次图状态、证据和预算回执 | 继续同一 run，保留旧反馈及缓存来源 |
| [本机增量调度](INCREMENTAL_REVIEW.md) | 完成基线与更新路径 | 优先阅读更新路径，保留整个 PR 范围；云端未共享基线 |

## 2. 来源与失效条件

采用单独的有界 JSON artifact，复用 `cloud_state.download_trusted_artifact()` 的来源检查。读取时核对 GitHub 数字仓库 ID、默认分支、工作流 ID/路径、已完成 attempt、事件类型、artifact 所属运行、有效期及 GitHub 下载摘要。只允许受信任的 `workflow_dispatch` / `pull_request_target` 工作流来源，PR 内容作为数据读取，不执行贡献者脚本。

外层 ZIP 只允许 `syntax-cache.json` 一个普通成员。可接受的下载 ZIP 与展开 JSON 均不得超过 8 MB，最多 128 条，每条输出最多 8,000 字符；通用下载器另有 32 MB 的硬读取上限，缓存入口在下载前核对声明大小、下载后收紧到 8 MB。拒绝重复 JSON key、重复条目、额外字段、非法路径和错误结果类型。整个批次通过校验才事务写入本机 SQLite；不下载或导入 SQLite、pickle，也不导入旧基线。

```text
key = SHA256(kind + repo_id + path + source_sha256 + compiler)
compiler = Python 完整版本 + optimize 级别 + checks.py 摘要
```

缓存元数据还要求相同 Harness 固定提交。因此更换 Harness 固定提交会冷启动；更新 Python、检查实现、文件路径、源码内容或仓库身份也会导致拒绝/未命中。模型或人工反馈变化不会改变纯编译结果的 key。文件在新提交中保持相同内容时可以命中，但报告的 `sha` / `version` 会重绑定到当前版本，另存 `origin_sha`、`origin_run_id` 和下载来源 `origin_artifact`。

这项设计参考 GitHub 的[受信任工作流安全说明](https://docs.github.com/en/actions/reference/security/secure-use)与[artifact 来源、摘要和下载接口](https://docs.github.com/en/rest/actions/artifacts)。JSON 格式、缓存规则和降级行为由本项目实现。

## 3. 开启与排查

在业务仓库部署更新后的固定 SHA 工作流，配置：

| Variable | 值 | 作用 |
| --- | --- | --- |
| `HARNESS_SYNTAX_CACHE_ENABLED` | `true` | 添加 `github-ci --cloud-syntax-cache` 并保存缓存 artifact |
| `HARNESS_CHECKPOINT_ENABLED` | 可选 `true` | 与语法缓存独立，可同时开启；恢复仍仅支持预览 |
| `HARNESS_PUBLISH` | `false` | 当前验收保持预览 |

查看报告 artifact 中：

- `syntax-cache-import.json`：`imported` / `cold` / `unavailable`，来源运行、条目数、被跳过的来源数。
- `review.json → syntax_cache`：当次证据里的命中/未命中，以及创建审查时冻结的导入来源。
- `evidence[].base/head.cache.origin_artifact`：具体复用结果的下载来源。
- `syntax-cache-export.json`：导出条目数与 JSON SHA256。

缺失、过期、来源不符、解析失败、摘要不符或接口不可用时不采纳缓存，继续编译当前文件。只能复用确定性的 passed/failed；unavailable、超限和非 UTF-8 文件不缓存。仅选择成功工作流中的缓存，失败审查保存的缓存不会成为自动候选。

恢复已完成 checkpoint 时，缓存统计保留原回执含义，不表示本次恢复新查询了多少次缓存。恢复不重新导入最近缓存；外部缓存丢失也不妨碍回放已完成检查。

## 4. 学习源码

阅读顺序：工作流 → `cli.py` → `cloud_cache.py` → `cloud_state.py` → `incremental.py` → `checks.py` → `runtime.py` / `report.py`。

`test_cloud_cache.py` 覆盖独立存储目录中的传输、阻止重复编译、fresh model / finding ID、SHA 重绑定、源码变化、非法批次拒绝、artifact 信任门槛、近期来源降级、CLI 接入及 checkpoint 在缓存消失后的回放。原 `test_incremental.py` 继续覆盖路径/仓库/编译器隔离、不可读文件和单元测试不复用。

面试时应说明：语法编译通常很便宜，这项功能主要展示缓存的身份、来源、失效与可追溯设计。命中不能推出模型调用、token 或总耗时下降，也不能证明审查准确率提升。

## 5. 运行边界

保留 7 天；只寻找最近 10 个已完成工作流，且每个 bundle 最多 128 条。并发运行各自保存 immutable artifact，后续选择一个最近有效来源；没有集中合并、跨 job 增量基线或持久队列。近期窗口内没有可用来源就冷启动。

直接 Re-run 可能删除来源 artifact；缓存可重新计算，但 checkpoint 恢复仍应新建 Run workflow。硬取消或机器消失不保证当前缓存导出成功。此处没有总耗时、费用或模型质量收益结论。

## 6. 2026-10-03 验收

完整本机回归 **343 项通过，150.19 秒，包含 Docker**；新增缓存专项 32 项通过。Ruff 检查、改动源码格式检查、模板及实际部署提案的 actionlint、差异空白检查通过。全仓格式检查指出 7 个已有文档/评测/测试文件的格式差异，本次未改动这些历史文件。

PharosRAG 部署提交 `b6a25454cdee9f96da579f47046d15698fa33977`，工作流 blob `e8463a5d6dd0ae1bd0cccb95847d0ffba61a6215`，固定 Harness `06467a1a2a09fbfa8d8d9c630087431153d56fd3`。源码及提案与远程字节一致，缓存开关已开启，发布仍关闭。测试对象为 [Draft PR #5](https://github.com/Bin-poco/PharosRAG/pull/5)，base/head 全程未变化。

| 独立 Actions 运行 | 导入状态 | 缓存命中 / 未命中 | 累计模型 / 工具尝试 | 导出条目 |
| --- | --- | --- | --- | --- |
| [首次冷启动 37130733694](https://github.com/Bin-poco/PharosRAG/actions/runs/37130733694) | cold | 0 / 1 | 9 / 14 | 1 |
| [新审查复用 37130841827](https://github.com/Bin-poco/PharosRAG/actions/runs/37130841827) | imported，来源为上一运行 | 1 / 0 | 7 / 11 | 1 |
| [完成结果恢复 37130959661](https://github.com/Bin-poco/PharosRAG/actions/runs/37130959661) | 不重新导入，回放原凭据 | 保留 1 / 0 | 保留 7 / 11，新增 0 / 0 | 0 |

首次和第二次属于不同 Harness run，均为真实 DeepSeek 新审查，finding ID 重新生成。新增演示文件在 merge base 不存在，所以 base 为 unavailable，只有 head 的编译结果可复用。第二次的 key、源文本摘要、编译器、原 run ID/SHA 及 `origin_artifact` 与首次对应，报告仍绑定当前 head。两轮均报告该演示文件的空输入契约问题，独立核验为 supported；语法编译通过本身并不证明这种运行时边界正确。

第三个 job 恢复第二次的完成状态，外部缓存没有导入，导出条目为 0；保留模型/工具累计预算、证据、trace、上下文、finding 及原缓存来源，新增模型和工具调用均为 0。这验证了缓存与 checkpoint 的兼容性。

已逐个核对三轮共 9 个 artifact 的 GitHub 摘要，核对 checkpoint 内部成员摘要及冻结 artifacts 摘要。扫描 54 份成员/日志，未发现本机已知 API/GitHub 密钥或常见密钥格式。PR 保持 Draft、未合并，review、行内评论、普通评论均为 0。收集过程中有一次本地下载因摘要不符被拒绝；重新独立下载后全部 9 个摘要通过，未确定初次差异的原因，未放宽摘要校验。

长期证据见 [cloud-syntax-cache-20261003.json](validation/cloud-syntax-cache-20261003.json)。本机原始证据在 `outputs/cloud-syntax-cache/`，未上传；artifact 会到期。内容变化、损坏及编译器失效由离线测试覆盖，本次云端没有修改 PR head。两轮模型/工具调用数不同，不能把这个差异归因于缓存，也没有建立总耗时、token、费用或审查质量收益。
