# 跨次云端审查的人工反馈记忆

更新时间：2026-10-03。版本化反馈文件、导入/导出与 GitHub 读取已实现，PharosRAG 两次独立云端运行均通过验收。运行图的跨 job checkpoint 恢复仍待实现。

## 1. 存储与运行路径

```mermaid
flowchart LR
    H[维护者明确确认反馈] --> S[本地 MemoryStore]
    S --> E[导出完整反馈历史]
    E --> G[业务仓库默认分支 feedback.json]
    G --> P[解析并固定默认分支 SHA]
    P --> V[校验仓库身份、格式和文件摘要]
    V --> D[本次 job 的 SQLite]
    D --> R[按路径、状态和有效期召回]
    R --> F[冻结快照与请求装配]
    F --> A[Agent 审查与报告]
```

业务仓库的 `.harness/feedback.json` 是共享反馈来源；SQLite 继续负责本机维护和本次运行的筛选。云端每个 job 从空数据库导入同一文件，实现跨运行读取。没有增加新的 Agent 框架或向量数据库。

这轮只持久共享人工反馈。运行消息、工作事实、执行回执与 checkpoint 仍保存在本机或当次 runner；失败或取消的云端 job 不会自动在下一台 runner 恢复。

## 2. 身份、版本与生命周期

| 字段/机制 | 用途 |
| --- | --- |
| `schema_version=1`、`kind` | 明确文件协议，拒绝未知结构 |
| `repo_id` | 必须对应 `sha256("github:<数字仓库ID>")`，阻止导入到其他仓库 |
| `uid` | 稳定 32 位十六进制 ID，跨数据库保留；本地数字 `id` 只供 CLI 管理 |
| `source_run_id` / `finding_id` / `source` | 追溯人工反馈关联的运行、意见与确认来源 |
| `path_glob` / `expires_at` / `status` | 限定生效文件、期限和状态 |
| `replaced_by` | 用 successor UID 保留修订历史 |
| 提交 SHA、blob SHA、文件 SHA256 | 追溯实际读取的文件版本与原始字节 |
| canonical bundle SHA256 | 排序与格式无关的逻辑内容摘要 |

导出包含完整历史，保留已过期、已撤销、已替换的记录。修订生成新 UID，旧记录标为 superseded；撤销保留原记录。召回仍只使用范围匹配、未过期的 active 记录。accepted 是人工确认的范围内指导，dismissed 只说明建议被驳回。

导入在单个 SQLite 事务中完成。已有记录不得丢失或原地改写；已有终止状态不能复活。本地未导出的编辑会阻止远程刷新；若输入恰好等于已发布的本地完整状态，可以重新确认同步。CLI 修改同步过的数据库后，报告的 `storage.local_changes=true`，远程来源只表示编辑前的基准。重新导入相同的已发布状态后标记恢复为 false。

上述历史保护适用于同一数据库的连续刷新。全新的云端数据库以可信默认分支的文件为准，不能独立发现 Git 历史中曾经出现过的撤销状态。维护者应通过正常 Git 审核保留历史，回退规则也应新增明确修订，而不是删除旧记录。

## 3. 维护者操作

先从业务仓库可信默认分支获取文件，导入专用的维护数据库。首次尚无反馈文件时，直接使用 `memory add` / `memory feedback` 创建记录。

```bash
uv run pr-harness memory import \
  --pr https://github.com/OWNER/REPO/pull/123 \
  --memory-db /你的/维护目录/feedback.sqlite3 \
  --bundle /你的/业务仓库/.harness/feedback.json

uv run pr-harness memory feedback \
  --pr https://github.com/OWNER/REPO/pull/123 \
  --memory-db /你的/维护目录/feedback.sqlite3 \
  --report outputs/已确认的审查/review.json \
  --finding-id finding-实际ID \
  --text '维护者明确确认的规则' --source '确认来源' \
  --scope 'examples/**/*.py' --rule-key python.empty-input-contract \
  --disposition accepted --ttl-days 90

uv run pr-harness memory export \
  --pr https://github.com/OWNER/REPO/pull/123 \
  --memory-db /你的/维护目录/feedback.sqlite3 \
  --out /你的/业务仓库/.harness/feedback.json
```

用现有 `memory list` 查本地数字 ID，再通过 `memory revise` 或 `memory revoke` 修改生命周期；命令同样指定这个 `--pr` 与 `--memory-db`。修改后再次导出，检查 Git diff，再提交到默认分支。源仓库公开时，这个文件也公开，因此只保存准备公开的反馈。

多人维护时使用 Git 的正常合并流程；出现反馈文件冲突，应在合并后的完整历史上重新导入、修订、导出。此版本不提供远程自动合并、反馈网页或 GitHub 评论采集。Agent 的模型意见不会自行写入此文件。

## 4. 云端启用

业务仓库先部署包含本功能的固定 Harness 提交，并提交有效的 `.harness/feedback.json`，再设置 Actions Variable：

| 变量 | 值 |
| --- | --- |
| `HARNESS_MEMORY_ENABLED` | `true` |
| `HARNESS_ENABLED` | `true` |
| `HARNESS_PUBLISH` | `false`，验收使用预览 |

新版模板把 `HARNESS_MEMORY_ENABLED=true` 转为 `github-ci --github-memory`。手动调用 `github-review` 也可添加 `--github-memory`；自定义 JSON 路径使用 `--github-memory-path`。没有开启时继续使用原本的本地 SQLite。

读取步骤发生在模型创建之前：

1. 确认 Snapshot 绑定本次 GitHub 数字仓库 ID。
2. 查询仓库默认分支，并将其解析为完整 commit SHA。
3. 使用这个 SHA 读取 JSON 文件，核对 API 返回的路径、编码、大小和 Git blob SHA。
4. 验证完整反馈结构和仓库身份，原子导入，再按本次改动路径召回。

PR 贡献者的 head 不参与记忆读取。审查 base/head 与记忆来源 SHA 分别记录；默认分支可能在获取 PR 后更新，因此两者不要求相等。文件最大 1 MB、最多 1,000 条历史记录；文字、路径、时间、关联字段也有上限。开启后文件缺失、无访问权限、身份或摘要不匹配都明确失败，不静默退回空记忆。

依照 [GitHub Contents API](https://docs.github.com/en/rest/repos/contents?apiVersion=2022-11-28) 使用 commit `ref` 和内容摘要。审查只增加 GET 请求，contents:read 即可；写入通过维护者的 Git 提交完成。自动流程继续仅读取代码和做语法检查，凭据边界见 [GitHub pull_request_target 安全说明](https://docs.github.com/en/actions/reference/security/securely-using-pull_request_target)。

## 5. 怎样检查实际生效

artifact 中新增 `memory-source.json`，记录 repository、branch、commit、path、blob、文件和逻辑内容摘要及历史记录数。

`review.json` 的 `context.memory_snapshot` 保存冻结召回文本、UID、范围、来源与 storage 回执。`context.assembly` 保存各次请求的记忆展示文本和截断信息。仅有“文件已下载”或“记录已召回”还不能说明模型看到了规则；验收应核对实际请求的 `memory_display_text`。

相关实现和测试：

- [memory_bundle.py](../src/pr_review_harness/memory_bundle.py)：协议、内容摘要、可信默认分支读取。
- [memory.py](../src/pr_review_harness/memory.py)：稳定 UID、生命周期、导入冲突检查与冻结召回。
- [cli.py](../src/pr_review_harness/cli.py)：导入/导出命令和模型调用前的入口。
- [test_memory_bundle.py](../tests/test_memory_bundle.py)：独立数据库、旧数据库迁移、刷新撤销、异常不部分写入、CI 请求可见性。

这证明人工反馈可以跨次读取。规则改善了多少遗漏或误报，仍需历史 PR → 后续 PR 的独立样本和消融实验；同一个演示 PR 的重复运行不能作为质量提升数据。

## 6. PharosRAG 两次独立云端验收

维护者已明确确认并保存：**“审查 examples/**/*.py 时，核对函数声明的空输入行为与实现是否一致。”** 范围限定为演示文件，accepted，有效期 90 天至 `2027-01-01T02:15:41.214546+00:00`。它关联此前 run `07a6f966d40742ce979788d97ce68abf` 的 finding `finding-fa36a3e2bb081d13`，不是 Agent 自动生成的记忆。

| 项目 | 本次固定版本 |
| --- | --- |
| Harness 源码 | [`ff9a0b1c881afe690e77124c59d7cb39bc218d4d`](https://github.com/Bin-poco/pr-review-harness/commit/ff9a0b1c881afe690e77124c59d7cb39bc218d4d) |
| 业务工作流与反馈文件 | [`e07afdc216ea70a98aa1870e9e23c349ac27c94e`](https://github.com/Bin-poco/PharosRAG/commit/e07afdc216ea70a98aa1870e9e23c349ac27c94e) |
| 反馈文件 | [`.harness/feedback.json`](https://github.com/Bin-poco/PharosRAG/blob/e07afdc216ea70a98aa1870e9e23c349ac27c94e/.harness/feedback.json) |
| 反馈 UID | `e2ec47adf4d64cd4b5239218a593e45a` |
| 测试 PR | [PharosRAG #5](https://github.com/Bin-poco/PharosRAG/pull/5)，Draft、开放、未合并 |
| PR base / head | `40136cea7cac09b3b9306f87ce9a3b69b1a2410f` / `80eb69da95f03d3e84a2b3905b887180269110ed` |
| 文件 SHA256 | `63c521e3576ce8ad504b3010449c3085757d944d414f53c1365230c0f4cf4122` |

两轮均为单独的 workflow_dispatch，使用新的 hosted job 和不同的 review run ID，没有恢复 checkpoint。日志核对了实际检出的 Harness SHA；反馈来源固定默认分支提交，与本次 API 返回的 PR base/head 分别记录。

| 验收结果 | [第一轮](https://github.com/Bin-poco/PharosRAG/actions/runs/37089473764) | [第二轮](https://github.com/Bin-poco/PharosRAG/actions/runs/37089583454) |
| --- | --- | --- |
| 状态 | success | success |
| job 时间（含安装与上传） | 22 秒 | 30 秒 |
| 规则在主审查请求中实际展示 | 3 / 3 | 10 / 10 |
| 来源 SHA、文件摘要、反馈 UID | 一致 | 一致 |
| 冻结召回文本摘要 | 一致 | 一致 |
| 审查结果 | 第 6 行 1 条 P2 | 第 6 行 1 条 P2 |
| 独立模型核验 | completed，supported | completed，supported |
| 主审查及核验模型 / 工具尝试 | 6 / 9 | 13 / 17 |
| 已报告 input / output tokens | 21,712 / 1,132 | 64,238 / 1,419 |
| 提交修复次数 | 0 | 0 |
| 发布 | preview | preview |

远程复核：reviews、行内评论、普通评论均为 0；自动发布保持 false；测试 PR 未合并。云端只做语法检查，不执行 PR 测试。第二轮模型额外使用虚拟文件系统的 ls/glob 查询仓库路径，返回空结果后才继续提交，导致调用数增加；这些额外调用被预算记录。当前不能据此声称调用量、成本或审查质量改善。

本机完整回归 **203 项通过**（包含 Docker），新增记忆专项 30 项通过；Ruff lint、修改文件格式、模板及业务工作流 actionlint、diff 检查通过。检查了本次公开文件、云端 artifact 与日志，未发现本机模型密钥或常见凭据格式。原历史评测封存文件未改写。

完整摘要与 artifact 哈希见 [cloud-memory-20261003.json](validation/cloud-memory-20261003.json)。本机原始 artifact 位于 `outputs/cloud-memory/first/` 和 `outputs/cloud-memory/second/`，Git 忽略；Actions artifact 保留期为 7 天。

建议下一轮先明确虚拟技能/记忆文件与仓库代码工具的职责，减少无效探索，再实现有版本失效规则的增量审查。跨 job checkpoint 恢复、远程反馈编辑界面和质量收益验证仍是独立任务。
