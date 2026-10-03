# 跨次云端审查的人工反馈记忆

更新时间：2026-10-03。已实现版本化反馈文件、导入/导出与 GitHub 读取；PharosRAG 两次独立云端运行的验收结果将在本文追加。运行图的跨 job checkpoint 恢复仍待实现。

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
