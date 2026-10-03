# 第一版验收记录

以下按改造时间保留历史记录。提交修复、GitHub 接入与真实故障重放见 [第一阶段](LANDING_V1.md)，Docker 执行与自动事件入口见[第二阶段](LANDING_V2.md)；旧章节中的“尚未实现/验证”表示当时状态。

日期：2026-10-02。Python 3.14.4，Deep Agents 0.7.21；完整版本见 uv.lock。

- `uv run pytest -q`：57 项通过。覆盖固定 SHA、merge base、变更行、路径/导出限制、上下文预算、记忆持久化/范围/过期、真实工具循环与提交结束、检查对照、无效证据、去重、预算失败保留轨迹。
- `uv run ruff check src tests`：通过。
- `uv run ruff format --check src tests`：通过。
- `uv run pr-harness demo --out outputs/first-run`：成功生成 Markdown/JSON 报告。公开折扣测试在 merge base 通过、head 断言失败。
- 工具循环测试确认：记忆内容进入实际模型系统消息；最终提交位于最后一个允许的模型调用时仍能正常结束。

示例报告位于 `outputs/first-run/review.md`。默认演示使用预设工具调用模型，审查结论预先给定。以上结果证明本地工程链路可运行，**不证明模型发现缺陷的能力或准确率**。

尚未验证：真实服务端 API 兼容性、LLM 审查效果、大型仓库性能、长上下文摘要触发、GitHub 发布和生产部署。

## 第二轮改造验收

同日加入 AST 符号关联、按需技能、独立核验和本地评估器后：

- `uv run pytest -q`：77 项通过。新增覆盖 AST 调用方/测试位置、原启发式对照、技能正文按需读取、第二轮核验的两版读取及预算耗尽、固定快照人工标注匹配与无效证据诊断、完整命令行流程。
- `uv run ruff check src tests`、`uv run ruff format --check src tests`：通过。
- `uv build`：成功生成源码包与 wheel，wheel 包含两个审查技能文件。
- `uv run pr-harness demo --verify --out outputs/current-demo`：生成审查报告；两版公开测试分别通过/失败，第二轮核验重新读取了 head/base。
- `uv run pr-harness evaluate --report outputs/current-demo/review.json --gold outputs/current-demo/demo-gold.json --out outputs/current-demo`：生成 `evaluation.json`。演示标注只验证评估命令，不代表模型质量。

改造机制已通过离线和边界测试。DeepSeek 官方接口已完成首轮真实审查调用；独立核验的实际误判率和上下文策略的质量收益仍未测量。以下为当时的首版验收记录；当前真实运行的结果见[首轮记录](../evaluation/click_real_prs/FIRST_RUN.md)。报告的两个阶段 Token 数仅覆盖模型返回的可见消息，内部摘要与重试未计入。

## 第三轮：参考对照、工作状态与记忆生命周期

- 官方文档与源码对照见 [设计记录](CONTEXT_MEMORY_DESIGN.md)，记录直接复用、机制借鉴、自主实现及尚未采用部分。
- `uv run pytest -q`：86 项通过。新增原生历史压缩后事实状态保留、状态上限/省略计数、反馈只读、旧数据库迁移、原子修订/撤销、跨仓库拒绝、记忆快照与 CLI 生命周期验证。
- `uv run ruff check src tests`、`uv run ruff format --check src tests`：通过。
- `uv run pr-harness demo --verify --out outputs/context-memory-demo`：成功；工作状态刷新 3 次，独立核验完成。报告保存初始上下文、记忆快照及状态刷新统计。

压缩测试使用刻意丢失细节的预设摘要模型，真实执行 SDK 压缩流程，验证下一轮模型输入仍包含原始工具证据；不表示已验证真实 LLM 的长上下文表现。演示仍为脚本结论。截至第三轮，尚无持久 checkpoint/进程崩溃恢复；第四轮补上该能力。计数始终不代表精确服务端计费。


## 第四轮：统一装配、预算、持久恢复与反馈关联

- `uv run pytest -q`：**109 项通过**（86 项改造前基线 + 23 项新增）。覆盖完整输入的系统/技能/schema/记忆计数，小窗口拒绝、真实原生压缩、摘要尝试累计、已完成结果跨进程恢复、检查未知执行、回执重放、批量工具状态合并、版本/预算/artifact 不兼容拒绝，以及 run 锁。
- 特别覆盖临时文件回执已保存而图尚未提交，以及摘要文件外置后模型失败：缓冲本步骤的文件变更，恢复仍能继续，不让部分 channel write 把失败步骤误标为已完成。
- 反馈测试覆盖 source_run_id/finding_id 关联、默认意见文件范围、rule_key 同主题分组、原子修订/撤销与无效来源拒绝。
- 四组消融测试使用同一快照与冻结记忆，输出位置候选指标和空白人工根因判断；labels 不进入 Agent 输入。
- `uv run ruff check src tests`、`uv run ruff format --check src tests`、`git diff --check`：通过。
- `uv build`：源码包与 wheel 构建成功。

最终命令行演示：

```bash
uv run pr-harness demo --verify --run-id context-memory-v3 \
  --runs-dir outputs/context-memory-v3/runs \
  --memory-db outputs/context-memory-v3/memory.sqlite3 --out outputs/context-memory-v3
uv run pr-harness resume --run-id context-memory-v3 \
  --runs-dir outputs/context-memory-v3/runs --out outputs/context-memory-v3/restored
uv run pr-harness verify --report outputs/context-memory-v3/restored/review.json \
  --out outputs/context-memory-v3/verified
uv run pr-harness benchmark --cases outputs/context-memory-v3/demo-cases.json \
  --out outputs/context-memory-v3/ablation --scripted
```

演示保存为 `outputs/context-memory-v3/review.md`。审查与核验均完成；共 6 次模型尝试、6 次工具尝试。恢复与再次核验保持证据、意见、预算请求记录和累计次数一致，没有额外模型/工具调用。四组 scripted 配置均完成；该公开命令样例没有历史记忆输入，含非空冻结记忆的对照另由测试覆盖。

脚本模型无服务端 token，用量 6 次均标为 unknown，没有当作零费用。预设结论与人工构造的演示标签只证明链路。首轮真实模型开发集已运行，但长 PR 表现、记忆迁移收益、提供方重试费用和可泛化的审查质量仍未测量；GitHub 发布、执行沙箱与增量审查尚未完成。

## 第五轮：提交可靠性与手动 GitHub 接入

- `uv run pytest -q --tb=short`：**150 项通过**。新增覆盖预算内提交修复、错误/恢复轨迹、GitHub 预览、版本过期、发布去重、未知响应、分页和远程反馈身份。
- 原批次三个提交失败用真实 DeepSeek 重放，三个都有效提交；其中两例再次截断后经修复完成，另一例未复现原参数错误。
- 公开 Click #2818 完成真实获取到本地报告；有效提交空意见，不代表不存在缺陷。
- PharosRAG Draft 测试 PR #4 完成真实 COMMENT 与第 6 行评论发布；原回执和空回执目录重跑均查回同一审查。远程确认只有一条审查、一条行内评论。
- `uv run ruff check src tests evaluation/submission_replay.py`、本次修改文件格式检查、`git diff --check`：通过。执行容器、自动触发和增量审查待完成。

本次原始路径、证据边界和当前联调状态见 [LANDING_V1.md](LANDING_V1.md)。旧批次与 freeze 未改写；新源码下的旧版本身份校验不能当作当前结果。

## 第六轮：容器执行与云端预览

- 本机全量 173 项通过、Docker/事件专项 23 项通过，真实容器和恢复记录见 [LANDING_V2.md](LANDING_V2.md)。
- 本仓库 Draft PR #1 完成真实 DeepSeek 云端手动预览：1 条 P2，第 6 行定位；独立核验 completed/supported；6 次模型、9 次工具尝试，提交修复 0 次。
- 自动 pull_request_target 事件正确跳过 Draft；手动运行仅做语法检查，发布 preview，远程审查和评论均为 0。
- Actions 运行链接与 artifact 摘要见 [CLOUD_ACCEPTANCE.md](CLOUD_ACCEPTANCE.md)。人工样例只用于工程验收；非 Draft 自动审查、业务仓库部署、跨 job 存储和质量收益仍需继续验证。


## 跨次云端人工反馈验收（2026-10-03）

在业务仓库默认分支保存维护者确认的版本化反馈，单独启动两次云端 DeepSeek 审查。两轮使用不同 hosted job 与 run ID，均读取相同反馈 UID、文件摘要和固定来源 SHA；规则实际出现在 3/3 与 10/10 次主审查模型请求中。均完成审查与独立核验，保持预览，Draft PR 未合并。

本机完整回归 203 项通过（含 Docker）；新增记忆专项 30 项通过。实现、生命周期与运维步骤见 [CLOUD_MEMORY.md](CLOUD_MEMORY.md)，原始结果摘要见 [cloud-memory-20261003.json](validation/cloud-memory-20261003.json)。这项验收仅证明人工反馈的跨 job 读取；云端 checkpoint 恢复和记忆质量收益尚未验证。以上历史记录保留原验收时间与结论。

## 运行中按文件召回反馈（2026-10-03）

- `HARNESS_TEST_DOCKER=1 uv run pytest -q`：**250 项通过，95.87 秒**；动态记忆专项 10 项。
- 新增覆盖冻结候选的仓库/生命周期隔离、候选数量与 UTF-8 容量限制、真实读取/搜索激活、列文件和失败读取不激活、数据库删除后恢复、原生摘要后反馈重建、虚拟文件不暴露全池、完整预算与增量配置失效。
- `uv run ruff check src tests scripts`、修改文件格式检查、`git diff --check`：通过。
- 真实 DeepSeek 主审查与独立核验完成：PharosRAG PR #5 的确认规则展示于全部 4 次主审查请求；第 6 行 P2，核验 supported，7 次模型与 10 次工具尝试。仅生成本地报告，未更新业务云端工作流。

真实案例只有一条已匹配规则，新相关文件规则的激活由工程测试验证。设计、源码顺序及输入边界见 [DYNAMIC_MEMORY.md](DYNAMIC_MEMORY.md)；原始摘要见 [dynamic-memory-20261003.json](validation/dynamic-memory-20261003.json)。没有记忆质量或成本收益结论。

## 统一代码片段索引与实际输入记录（2026-10-03）

- `HARNESS_TEST_DOCKER=1 uv run pytest -q --tb=short`：**267 项通过，98.32 秒**；片段索引专项 17 项。
- 新增覆盖 merge base/head 与改名旧路径、增删文件、分散邻域、初始裁剪与部分读取、搜索长行、精确片段去重、符号分析上限、索引记录/来源/字节上限、Unicode/CRLF 的 Git 行号、非普通文件元数据，以及回执完成而图未提交时的恢复。
- 原有真实图压缩测试增加断言：摘要后初始消息不再出现时，展示字符数为 0；片段引用与字面原文分开记录；重建图后索引一致。
- `uv run ruff check src tests scripts`、修改文件格式检查、`git diff --check` 与文档本地链接检查：通过。
- 真实 DeepSeek 审查与独立核验完成：PharosRAG PR #5，第 6 行 P3，核验 supported。初始材料、`read_code`、`search_code` 三类来源合并为 3 个片段；3 个完整代码摘要均与固定 Git 版本核对一致，全部 4 次主请求的片段 ID 可追溯且完整请求预算通过，人工规则仍展示于 4/4 次主请求。
- 7 次模型、10 次工具尝试；已报告输入 27,857、输出 1,213、合计 29,070 tokens。按相同 `--thinking-mode disabled` 配置跨进程恢复，索引、请求、证据、核验与累计用量一致，无新增模型/工具调用。

仅生成本地报告，未发布远程审查或升级业务 Actions 工作流。真实案例为小型新增文件 PR，长历史压缩和部分输出边界由受控测试验证；没有质量、成本或减少重复读取的收益结论。当前索引覆盖主审查，独立核验仍保留独立读取记录。设计与学习顺序见 [CONTEXT_FRAGMENTS.md](CONTEXT_FRAGMENTS.md)，验收摘要见 [context-fragments-20261003.json](validation/context-fragments-20261003.json)。

## 独立核验复用片段索引（2026-10-03）

- `HARNESS_TEST_DOCKER=1 uv run pytest -q --tb=short`：**276 项通过，131.03 秒**；新增核验片段专项 9 项。
- 覆盖候选 diff 的真实位置与裁剪末行、LF/CRLF 和 Unicode 分隔符、读取完整前缀与部分末行、预算提示文字不扩展代码覆盖、阶段来源/反馈隔离、引用整条省略、独立进程恢复，以及核验回执完成而图未提交时的重放。
- 真实 SDK 压缩测试确认：初始候选材料移出请求后，展示源字符数为 0；源码引用仍可定位历史材料；字面代码输入只由当前真实材料/工具原文决定。
- `uv run ruff check src tests scripts`、修改 Python 文件格式检查、`git diff --check` 与文档本地链接检查：通过。
- 真实 DeepSeek 对 PharosRAG PR #5 完成审查与核验：第 6 行 P3，核验 supported。主阶段 3 个片段、3 次请求；核验阶段 1 个片段、3 次请求。两阶段全部 4 个完整代码摘要与固定 Git 版本一致，每次请求的索引摘要都能从当时 trace 前缀重建；人工规则展示于 3/3 次主请求。
- 6 次模型、9 次工具尝试；已报告输入 22,187、输出 1,185、合计 23,372 tokens。在两个新进程中分别执行主阶段恢复和核验恢复，索引、来源、请求、证据、意见、核验结论与累计用量一致，**没有额外模型或工具调用**。

核验采用相同 FragmentIndex 算法，独立保存 `verification.context`，不会把主审查的读取/反馈变为核验事实；没有新增独立索引数据库。实现与学习顺序见 [CONTEXT_FRAGMENTS.md](CONTEXT_FRAGMENTS.md)，来源代码提交、固定 SHA 与核对结果见 [verifier-fragments-20261003.json](validation/verifier-fragments-20261003.json)。

本次真实案例是小型新增文件 PR，base 文件不存在；两版均存在、长历史压缩与部分返回由受控测试验证。只生成本地报告，业务云端工作流仍使用先前部署版本；云端 checkpoint 恢复尚待完成。没有审查质量、费用或减少重复读取的收益结论。

## 上下文与记忆能力的云端部署升级（2026-10-03）

- PharosRAG 工作流仅更新 Harness 固定提交：`e12744e…` → `11f34dd…`，远程文件与提案字节及 Git blob 摘要一致。精确提案通过 actionlint；事件入口专项 **13 项通过，2.73 秒**。源码未变，既有 276 项完整回归记录保留，未重新运行全套。
- [Actions 37118915801](https://github.com/Bin-poco/PharosRAG/actions/runs/37118915801) 手动审查 Draft PR #5，completed / success。实际 checkout 日志和报告实现摘要与审核版本一致；保持 preview，远程三类评论均为 0，PR 未合并。
- DeepSeek 完成主审查及独立核验：第 6 行 P2，核验 supported。主阶段 3 个片段 / 4 次请求，核验阶段 1 个片段 / 3 次请求；全部完整片段源码摘要与固定 Git 版本一致，最终索引及每次请求的索引摘要均可由各阶段原始材料和当时 trace 前缀重建。
- 默认分支人工反馈进入冻结候选池，确认规则实际展示于 4/4 次主请求；来源 SHA、文件摘要和 UID 均核对一致。规则原本已匹配变更文件，新相关文件激活的分支仍由工程测试覆盖。
- 7 次模型、10 次工具尝试，已报告 30,191 tokens，unknown_usage_calls=0；仅做语法检查。核验错误版本参数经工具反馈纠正，错误读取没有产生源码片段。

说明与学习练习见 [CLOUD_CONTEXT_UPGRADE.md](CLOUD_CONTEXT_UPGRADE.md)，长期证据见 [cloud-context-upgrade-20261003.json](validation/cloud-context-upgrade-20261003.json)。本次没有触发原生摘要、验证云端中断恢复或建立质量/费用收益结论。云端 checkpoint 与跨 job 增量缓存仍待接入；上文各阶段本机记录保留原验收边界。

## 云端 checkpoint 保存与恢复（2026-10-03）

- 最终源码 `e15893bdc6dc4cad4d67e1e12d98261dc47a9758`，`HARNESS_TEST_DOCKER=1 uv run pytest -q --tb=short`：**311 项通过，140.01 秒**；新增云端专项 35 项。
- 真实图测试覆盖模型在完成检查后失败、核验回执提交而图尚未提交、完成结果恢复不新增调用、删除记忆数据库后仍保留原快照、WAL 页面备份、活动 run 锁、身份/预算失配、归档篡改、工作流来源与下载凭证隔离。普通运行反馈加载顺序的回归已修复，联合专项 78 项通过。
- `ruff check src tests scripts`、修改 Python 格式检查、`git diff --check` 与精确工作流提案 actionlint 通过。
- PharosRAG 固定最终源码，开启 checkpoint、保留预览模式。三次独立运行 **37128501642 → 37128615856 → 37128744336**：主审查核验前暂停并归档（4 模型/7 工具）→ 新运行复用主审查并继续核验（7/11）→ 新运行恢复完整结果（仍为 7/11，新增调用 0/0）。
- 贯穿相同 Harness run ID；主审查意见、证据、消息、完整上下文与冻结人工规则一致；后两次核验与执行回执一致（当前恢复耗时除外）。成员摘要、实际源码 checkout、全部 7 次请求的片段索引与固定代码摘要已核对；47 个日志/状态文件及内部成员凭证扫描通过。PR 保持 Draft 未合并，远程评论/审查均为 0。

真实 Re-run 发现上一 attempt artifact 消失，导致首次自动恢复设计失败；据此改成**新建 Run workflow 并指定来源**，明确拒绝直接 Re-run。云端采用阶段边界的受控暂停，正在执行时的故障由离线图测试覆盖；没有硬取消恢复、自动发布、跨 job 检查缓存或质量/成本收益结论。完整设计、操作与平台发现见 [CLOUD_RECOVERY.md](CLOUD_RECOVERY.md)，长期证据见 [cloud-recovery-20261003.json](validation/cloud-recovery-20261003.json)。

## 2026-10-03：跨 job 语法缓存与恢复兼容性

- 源码 `06467a1a2a09fbfa8d8d9c630087431153d56fd3`，完整回归 **343 项通过，150.19 秒，包含 Docker**；新增缓存专项 32 项。Ruff、改动源码格式、模板及部署提案 actionlint、差异空白检查通过；全仓格式检查提示 7 个已有文件差异，历史文件未改动。
- PharosRAG 固定版本部署后，三次独立 Actions 成功：冷启动 `37130733694` 为 0 hit / 1 miss；新审查 `37130841827` 导入前一运行，1 hit / 0 miss；完成结果恢复 `37130959661` 保留缓存凭据和累计预算，新增模型/工具均为 0，外部缓存导入跳过且导出条目为 0。
- 两次新审查模型均重新执行，run / finding ID 不复用。9 个 artifact 摘要及 checkpoint 内部成员摘要核对通过；54 份成员/日志已作凭据扫描。PR #5 head/base 未变，保持 Draft、未合并，三类评论数均为 0。

只共享纯编译结果，模型意见、单元测试和调度基线不共享。云端未修改 PR head，内容/编译器失效及错误来源拒绝由离线测试覆盖；没有耗时、费用或审查质量收益结论。收集时一次摘要不符下载被拒绝，重新独立下载后全部通过，原因未确定。设计、学习与完整验收见 [CLOUD_SYNTAX_CACHE.md](CLOUD_SYNTAX_CACHE.md)，长期证据见 [cloud-syntax-cache-20261003.json](validation/cloud-syntax-cache-20261003.json)。上文保留各阶段原始结论。
