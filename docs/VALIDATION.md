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
