# 云端上下文与记忆升级验收

日期：2026-10-03，Asia/Shanghai。将 PharosRAG 的调用方工作流升级到已完成本机测试的 Harness 版本，再用 DeepSeek 官方 API 完成一次云端预览。测试对象仍为人工演示 PR；这次验收检查部署、输入来源与工程链路。

## 1. 部署与结果

| 项目 | 本次记录 |
|---|---|
| 业务仓库部署 | [PharosRAG 提交 0a267a7](https://github.com/Bin-poco/PharosRAG/commit/0a267a76e507e555818827897053b6d973f4d772) |
| 工作流改动 | `.github/workflows/pr-review.yml` 仅更新一行 Harness 完整提交 SHA |
| 固定 Harness 源码 | `11f34dd7c0797b1ef550ea4da642a8e28a0a65f1`，此前为 `e12744e10440fbf3b3b4779687e0a0de693fbec0` |
| 云端运行 | [Actions 37118915801](https://github.com/Bin-poco/PharosRAG/actions/runs/37118915801)，`workflow_dispatch`，completed / success |
| 审查目标 | [PharosRAG PR #5](https://github.com/Bin-poco/PharosRAG/pull/5)，保持 Draft、未合并 |
| PR base / merge base | `40136cea7cac09b3b9306f87ce9a3b69b1a2410f` |
| PR head | `80eb69da95f03d3e84a2b3905b887180269110ed` |
| 模型 | `deepseek-flash`，官方接口，温度 0，关闭思考 |
| 意见与核验 | 1 条 P2，`examples/harness_auto_preview.py:6`；核验 completed / supported |
| 发布 | `preview`；运行后远程审查、行内评论、普通评论均为 0 |

意见指出 `mean_chunk_score([])` 与文档约定的返回 `0.0` 不一致：实现直接除以空列表长度。语法检查通过不证明运行时行为正确；本次未执行 PR 测试，核验结论仍是模型判断。

本次 7 次模型尝试、10 次工具尝试，已报告输入 28,838、输出 1,353、合计 30,191 tokens，unknown_usage_calls=0。主阶段提交修复 0 次。核验器曾使用无效版本参数 `merge-base`，收到工具错误后改为 `base`，确认新文件在 base 不存在后完成提交。参数错误也计入工具预算，不产生代码片段。

## 2. 三种版本怎样区分

| 版本 | 含义 | 本次位置 |
|---|---|---|
| 工作流所在业务提交 | 运行时采用的可信部署配置 | Actions 的 `headSha=0a267a7…` |
| 实际执行的 Harness 提交 | 运行哪些审查与核验代码 | checkout 的仓库与 `git log` 输出 `11f34dd…` |
| 被审查的 PR 提交 | 读取哪一版业务代码 | `review.json` 的 base / merge_base / head |

不能用 Actions 页面的 head 代替执行源码或 PR 版本。本次核对远程工作流字节与本地提案一致，Git blob SHA 正确；下载日志确认实际 checkout 到固定 Harness SHA。报告的 `implementation_sha256` 也与本机已审核源文件一致：`6a726cb69c6fd994d175fde9c63a3b00e16b73798dee929f52f8bade54e3ea99`。

Harness 主分支后续增加文档不会自动改变业务工作流的固定版本；升级运行代码仍需更新完整 SHA。

## 3. 上下文的验收

| 阶段 | 归档片段 | 请求数 | 来源 |
|---|---:|---:|---|
| 主审查 `review` | 3 | 4 | 初始 diff / 邻域 / 配置、自己的 `read_code`、自己的 `search_code` |
| 独立核验 `verification` | 1 | 3 | 候选意见的 diff、核验器自己的 `read_code` |

从固定 Git 对象重建初始 ContextPack，全文与云端报告一致；核验 packet 由同一固定 Snapshot 和保存的候选意见重建。随后逐项检查：

1. 两阶段全部 4 个完整片段的行范围与源码摘要均与固定 Git 版本一致。
2. 两阶段最终索引与重建结果完全一致，包括各自的 `origins`。
3. 7 次请求的 `fragment_index_sha256` 都能由当时 trace 前缀重建；可见片段 ID 和范围均能关联本阶段索引。
4. `initial_source_chars` 没有超过实际展示字符；请求均在 120,000 字符预算内。

同一完整代码片段可以在两阶段拥有相同 ID，来源仍分别记录。主审查的读取回执没有变成核验器的读取事实。索引是固定原始材料与工具回执的派生视图，云端也没有新增独立索引数据库。

这是 6 行新增文件的小案例，初始材料在所有请求中仍保留；本次没有触发原生历史压缩。压缩后引用与字面原文的区别、部分末行、两版均有文件等边界由[片段索引专项测试](../tests/test_verifier_fragments.py)及既有持久上下文测试验证。不要把归档片段数量当成审查覆盖率。

## 4. 记忆的验收

业务仓库保持 `HARNESS_MEMORY_ENABLED=true`。本次从默认分支 `0a267a7…` 读取 `.harness/feedback.json`，文件摘要仍为 `63c521e3576ce8ad504b3010449c3085757d944d414f53c1365230c0f4cf4122`。

维护者此前确认的规则：**审查 `examples/**/*.py` 时，核对函数声明的空输入行为与实现是否一致。**

- 冻结候选池包含 1 条记录，UID 为 `e2ec47adf4d64cd4b5239218a593e45a`；池摘要写入报告。
- 全部 4 次主请求的 `memory_recall.pool_sha256` 与冻结池一致，规则实际出现在 `memory_display_text`，不是只记在数据库里。
- 召回原因从 `changed-path match` 更新为真实 `search_code match`，仍对应同一演示文件。
- 核验沿用独立输入范围和自己的读取来源。

这个规则启动时已经匹配变更文件，因此本次没有证明“后来发现新相关文件时激活另一条规则”；该分支由[动态记忆测试](../tests/test_dynamic_memory.py)覆盖。更早的[跨 job 人工反馈验收](CLOUD_MEMORY.md)单独保留，当前一次运行不能替代两次独立运行的证据。

## 5. 测试与归档

本次精确工作流提案通过 actionlint，事件入口专项 **13 项通过**。执行源码未变，沿用升级前 **276 项完整回归通过（含 Docker）** 的记录，未将其写成这次重新运行的结果。

下载的五个 artifact 文件、日志和工作流均保存 SHA-256；模型密钥精确值及常见凭据格式扫描通过。长期摘要见 [cloud-context-upgrade-20261003.json](validation/cloud-context-upgrade-20261003.json)。完整报告与日志保存在本机被忽略的 `outputs/cloud-context-upgrade/`；GitHub artifact 保留 7 天。旧验收记录未改写。

## 6. 学习与下一阶段

先读[统一片段索引](CONTEXT_FRAGMENTS.md)与[动态记忆](DYNAMIC_MEMORY.md)，再按“业务工作流 → 固定源码 checkout → `automation.py` → 主请求 → 核验请求 → 发布预览”追踪这次运行。用长期摘要中的 `stages.*.records`、`requests` 与 `memory` 回答：

1. 为什么两阶段同一片段 ID 的来源不同？
2. 为什么语法通过仍会出现空输入除零意见？
3. 为什么查到数据库中的规则，还需要检查每次请求的实际展示记录？
4. 为什么工作流所在提交、执行源码提交和被审查提交是三个不同概念？

**下一项工程工作是云端 checkpoint 恢复。** 当前工作流只上传报告，job 结束后运行目录和 checkpoint 没有跨 job 保留；本机 `resume` 已验证。需要进一步设计受控状态归档、版本与实现身份校验、同 PR 并发处理和恢复入口，再验证中断后的重放。云端增量缓存也尚未接入；这次完成的是已有上下文与记忆能力的部署验收。
