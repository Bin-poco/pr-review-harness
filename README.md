# PR Review Harness

面向 Python 代码仓库的 PR 自动审查学习项目。基于 **Deep Agents 0.7.21**，围绕固定版本的代码变更组织上下文、调用检查工具、生成有来源的审查意见，并复用维护者明确确认的仓库经验。

## 当前可运行版本

| 模块 | 当前实现 |
|---|---|
| Agent 运行 | 真实 Deep Agents 模型 ↔ 工具循环；阶段与全局调用预算，包含摘要和失败尝试 |
| PR 输入 | 本地 Git base/head 或 GitHub PR 链接；固定 SHA，使用 merge base 归因，获取前后确认远程版本 |
| 上下文 | diff、变更邻域、配置；AST 关联调用方与测试；统一代码片段索引与每次实际输入记录；ContextManager 组装、预算校验；每轮刷新版本/证据与有限片段引用 |
| 证据与执行 | Python 语法检查；可选 unittest；双版本对照；Docker 固定镜像、无网络、只读与资源/日志/时间上限 |
| 记忆 | SQLite 持久保存人工反馈；按仓库、路径范围、有效期筛选；支持修订/撤销/替换历史、run/finding 来源、主题分组及冻结快照；版本化 JSON 支持云端跨 job 读取 |
| 持久恢复 | LangGraph SQLite checkpoint 保存消息、临时文件与事实状态；执行回执保存检查结果与累计用量；固定版本 resume |
| 提交可靠性 | 输出截断、无提交、参数与校验错误的预算内限次修复；保留原始输出与失败诊断 |
| GitHub 发布 | 默认本地预览，显式发布 COMMENT 与行内意见；版本校验、本地回执、远程标记去重 |
| 自动审查 | Actions 工作流与事件入口；可信源码、事件版本校验、同 PR 并发取消、默认预览；云端手动预览与 Draft 跳过已验收 |
| 审查技能 | 两个短 playbook 通过 Deep Agents Skills 按需读取，分别针对边界回归和 API 兼容 |
| 工具路由 | list_code_files 分页列出固定版本仓库文件；区分仓库读取与虚拟技能/记忆/笔记，误用路径给出纠正提示并保存回执 |
| 增量调度与缓存 | 更新路径优先进入上下文，保留完整 PR 范围；纯语法结果按源内容/编译器复用；配置、merge base 或祖先关系变化时回退 |
| 独立核验 | 可选第二轮 Agent，重新读取 base/head，对原意见给出支持、驳回或不确定的判断；复用片段索引，分别记录核验来源、摘要后输入与恢复历史 |
| 本地评估 | 将保存的报告与固定版本人工标注比较，输出位置命中候选指标、遗漏、重复和证据诊断；四组上下文/记忆消融入口 |
| 报告 | Markdown + JSON；head 行号、证据 ID、版本信息、工具轨迹、用量与耗时 |

**当前是可运行的工程原型。** GitHub 手动发布、Docker 执行和自动审查入口已实现；Actions 工作流经静态检查、事件入口经本机真实模型联调，并完成[云端手动预览与 Draft 事件跳过验收](docs/CLOUD_ACCEPTANCE.md)。已通过两次独立云端运行验收[版本化人工反馈的跨 job 读取](docs/CLOUD_MEMORY.md)，本机增量调度与语法缓存已实现；云端 checkpoint 恢复、跨 job 缓存和部署运维仍待完成。已完成 [3 个 Click PR 开发案例](evaluation/click_real_prs/FIRST_RUN.md)和 [18 个跨仓库公开 PR 的封存试跑](evaluation/independent_real_prs/FIRST_RUN.md)。原试跑中 36 次审查有 3 次未能提交有效结果，本次[故障重放](docs/LANDING_V1.md)三例均完成，其中两例观察到截断后的修复提交。位置命中与误报仍需逐条根因复核，不能作为准确率结论。AST 关联是静态启发式，记忆检索采用确定性筛选；第二轮核验也只是模型意见，不能替代人工判断。

## 运行示例

新增[工具路由设计与云端验收](docs/TOOL_ROUTING.md)：真实预览已使用仓库文件列表，两个技能与跨次人工规则正常加载；本机完整测试 216 项通过。该演示验证接入行为，尚无成本或质量改善结论。

新增[增量审查设计与学习路线](docs/INCREMENTAL_REVIEW.md)：`review` / `github-review` 添加 `--incremental` 可使用本机持久基线与语法缓存；模型结论和单元测试在新运行中重新产生，云端跨 job 共享尚未接入。

需要 Python 3.11+、Git、uv。克隆后在项目目录执行：

```bash
git clone https://github.com/Bin-poco/pr-review-harness.git
cd pr-review-harness
uv sync --group dev
uv run pr-harness demo --verify
```

示例会生成两个提交：原本正常的折扣计算和人为引入的整除错误，然后经过 **真实 Deep Agents 工具循环** 读取代码、执行两个版本的公开测试、提交审查结果并进行第二轮核验。输出目录位于 `outputs/demo-时间/`，包含代码仓库、`review.md` 和 `review.json`。

**默认 demo 的审查与核验模型都是预设工具调用器：结论预先给定，检查结果真实执行。它用于证明流程可运行，不代表 LLM 已经发现缺陷，更不是准确率评测。** 使用真实模型可执行 `uv run pr-harness demo --live --verify`。

## 接入你可用的模型

第一版使用支持工具调用的 Chat Completions 兼容接口。模型名、密钥和可选服务地址由你配置：

DeepSeek 官方接口可先复制配置示例，在本机填写密钥，再用 `uv run --env-file .env` 加载：

```bash
cp .env.example .env
chmod 600 .env
# 编辑 .env，填写 HARNESS_API_KEY
uv run --env-file .env pr-harness demo --live --verify --thinking-mode disabled
```

`.env`、运行输出、checkpoint 和记忆数据库均由 Git 忽略。也可使用环境变量：

```bash
export HARNESS_MODEL="你可用的模型名"
export HARNESS_API_KEY="你的密钥"
export HARNESS_BASE_URL="你的兼容接口地址"

uv run pr-harness review \
  --repo /你的/Python仓库 \
  --base main \
  --head feature-branch \
  --verify \
  --out outputs/my-review
```

如果使用客户端默认服务，可省略 `HARNESS_BASE_URL`。支持 `OPENAI_API_KEY` 作为密钥后备变量。不会自动读取 `.env` 文件；如将这些变量保存在本地 `.env`，先执行 `set -a; source .env; set +a`。首次真实调用需要检查服务是否支持当前工具参数格式。DeepSeek 官方 Chat Completions 接口可使用 `--model deepseek-flash --base-url https://api.deepseek.com --thinking-mode disabled`；关闭思考模式可以避免工具调用多轮对话中遗漏 `reasoning_content`，评测报告会记录该设置。

默认仅允许语法检查。`review` / `github-review` 添加 `--run-tests` 后，默认用 Docker 执行 Agent 选择的 unittest 文件。需要先启动 Docker 并执行 `docker pull python:3.12-slim`；没有镜像就报错。容器无网络、只读、非 root，并有资源与时间限制；不自动安装仓库依赖。受信任的本地代码可显式用 `--test-backend local`，远程测试拒绝该选项。离线 demo 与 Python API 为兼容原练习仍默认本机执行。详见[执行与自动审查指南](docs/EXECUTION_AND_AUTOMATION.md)。

```bash
uv run pr-harness demo --test-backend docker --verify --out outputs/docker-demo
```

`--verify` 会额外调用一个独立核验 Agent，默认最多核验 5 条意见，最多 24 次模型调用和 32 次工具调用。可用 `--verify-max-findings`、`--verify-model-calls`、`--verify-tool-calls` 调整。核验失败会在报告中标出，原审查意见仍保留。

`--context-strategy ast` 是默认上下文策略。要在相同字符预算下对照原有文件名/导入启发式，可将它改为 `--context-strategy imports`；`context` 命令对应参数是 `--strategy`。

预算集中在 `BudgetPolicy`：初始材料 24,000 字符、主阶段读取 40,000 字符、核验读取 20,000 字符、记忆 4,000 字符、工作状态 6,000 字符。已知模型窗口时，预留输出与安全余量，并计入系统策略、技能、工具 schema、记忆和全部有效历史；通过 `--window-tokens` 可显式配置窗口。未知窗口采用完整请求 120,000 字符上限，可用 `--request-chars` 调整。这两种模式均在报告中记录计数方式；适配器 token 计数仍是估计。

两个阶段共享默认最多 64 次模型尝试和 100 次工具尝试，分别可用 `--total-model-calls`、`--total-tool-calls` 调整。SDK 摘要、失败调用和恢复重试计入次数，恢复不重置。报告区分已知 token 与用量未知的调用；提供方内部重试可能不暴露用量，所以没有精确费用保证。

默认最多安排 2 次提交修复，`--submission-repairs 0` 可关闭。修复仅允许提交现有证据支持的意见，所有尝试仍计入阶段与全局预算；最终失败保存 `failed.json`，程序不会代填空意见。报告的 `submission` 字段记录触发原因、次数和结果。

## 审查 GitHub PR

```bash
uv run --env-file .env pr-harness github-review \
  --pr https://github.com/OWNER/REPO/pull/123 \
  --model deepseek-flash --base-url https://api.deepseek.com \
  --thinking-mode disabled --out outputs/github-review

uv run --env-file .env pr-harness github-publish \
  --report outputs/github-review/review.json --out outputs/github-preview
```

第二条命令仅生成预览；带 `--send` 才发布 COMMENT。发布前重新确认 PR 版本和意见位置，同一快照通过本地回执及远程标记防重复。手动 GitHub 审查可显式允许 Docker 测试；持有凭据的 `github-ci` 自动流程只做语法检查。凭据、反馈写入、重试未知结果与限制见 [GitHub 接入指南](docs/GITHUB_INTEGRATION.md)。

`.github/workflows/review-pr.yml` 提供 PR 事件与手动触发；默认需 `HARNESS_ENABLED=true` 才运行，`HARNESS_PUBLISH=true` 才自动发布。接入其他业务仓库时用 `scripts/install_workflow.py` 生成固定 Harness 提交的工作流。配置 Secret、部署到可信默认分支后才能在线使用，见[部署步骤](docs/EXECUTION_AND_AUTOMATION.md)。

本项目已发布到 [Bin-poco/pr-review-harness](https://github.com/Bin-poco/pr-review-harness)。首次配置步骤见 [GitHub 上线配置](docs/GITHUB_SETUP.md)。已完成[本仓库云端手动预览](docs/CLOUD_ACCEPTANCE.md)，以及[PharosRAG 业务仓库部署与非 Draft 自动预览](docs/PHAROS_CLOUD_ACCEPTANCE.md)；自动发布保持关闭。

## 中断后恢复

CLI 默认将每次运行存入 `.pr-harness/runs/<run_id>/`，输出会显示 run_id。可以先固定一个演示身份：

```bash
uv run pr-harness demo --verify --run-id learning-01 --out outputs/learning-01
uv run pr-harness resume --run-id learning-01 --out outputs/restored-01
uv run pr-harness verify --report outputs/restored-01/review.json --out outputs/verified-01
```

`resume` 恢复主审查，`verify` 启动或恢复核验。已完成阶段不会再次调用模型或执行检查。运行绑定 repo、SHA、模型配置、预算、技能与实现摘要、执行环境；这些变化需开始新 run。当次人工记忆已冻结，即使数据库随后修订，也不会改写旧 run 输入。

工具执行前先保存意图，完成后保存结果。中断留下意图但没有结果时标记 `execution_unknown`，添加 `--retry-unknown` 才允许重新执行；它不能保证外部调用恰好执行一次。JSON/Markdown 报告原子替换。运行目录含仓库材料与反馈；执行锁当前面向 macOS/Linux。

## 用人工标注检查结果

将一次真实审查的人工标注保存为 `gold.json`（格式见 [评测说明](docs/EVALUATION.md)），然后运行：

```bash
uv run pr-harness evaluate \
  --report outputs/my-review/review.json \
  --gold private/gold.json \
  --out outputs/my-evaluation
```

评估结果的路径/行号匹配只是候选指标，仍要人工核对缺陷根因和触发条件。脚本 demo 即使位置全对，也不能当作模型准确率。

首轮真实 PR 开发案例见 [Click 评测集](evaluation/click_real_prs/README.md)与[真实模型运行记录](evaluation/click_real_prs/FIRST_RUN.md)。跨仓库[公开试点评测集](evaluation/independent_real_prs/README.md)已有 Flask、Requests、Werkzeug、Click 的 18 个双版本可复现 PR，含 8 组回归与修复及 2 个独立对照。其公开来源、固定 SHA、复现方式和标注均可检查；公开标签不能当作私有测试集。

## 查看上下文，写入仓库反馈

```bash
uv run pr-harness context --repo /你的/仓库 --base main --head feature-branch

uv run pr-harness memory add \
  --repo /你的/仓库 \
  --text "折扣接口必须保留小数计算，覆盖 0、部分折扣和 100 三类边界。" \
  --source "PR-12-maintainer-feedback" \
  --scope "pricing.py" \
  --ttl-days 90

uv run pr-harness memory list --repo /你的/仓库

uv run pr-harness memory revise --repo /你的/仓库 --id 1 \
  --text "更新后的维护者规则" --source "PR-20-feedback" --reason "业务约定更新"

uv run pr-harness memory revoke --repo /你的/仓库 --id 2 --reason "此约定已废弃"
```

人工驳回的建议使用 `--disposition dismissed` 保存；它只代表此建议曾被驳回，不会自动推导出允许某种业务行为。Agent 无权把自己的结论写入持久记忆，也不能编辑临时仓库反馈文件。修订和撤销保留历史，影响后续审查；当次报告区分冻结召回记录和每次模型请求实际展示的记录，保存 ID、来源、截断和内容摘要。

CLI 先冻结本仓库有效反馈候选，再按 PR 改动路径与审查期间实际代码读取/搜索命中召回。候选最多 1,000 条、记录 JSON 最多 1 MB；每次只展示预算内的规则，来源与省略可追踪。恢复使用同一候选版本，详见[运行中按文件召回](docs/DYNAMIC_MEMORY.md)。

可将人工反馈关联到报告中的具体 finding（`--finding-id` 从 `review.json` 复制）：

```bash
uv run pr-harness memory feedback --repo /你的/仓库 \
  --report outputs/my-review/review.json --finding-id finding-实际ID \
  --text "维护者明确给出的反馈" --source "PR-12-maintainer" \
  --rule-key pricing.discount
```

`feedback` 默认记录 dismissed；若是明确认可的规则，指定 `--disposition accepted`。同一 `rule_key` 下多条适用反馈提示人工核对，表示同主题未决，不声称已经识别自然语言矛盾，也不按时间自动裁决。

默认数据库为当前目录 `.pr-harness/memory.sqlite3`；跨运行请从同一项目目录执行或使用相同的 `--memory-db` 绝对路径。本地 `--repo` 身份基于 Git common-dir 的真实路径，同一仓库的 worktree 共享记忆，另一个克隆默认独立。GitHub 模式按数字仓库 ID 识别；将记忆命令的 `--repo` 换为 `--pr PR链接` 即可使用同一远程身份，跨克隆复用反馈。两类身份不会自动合并。记忆与审查轨迹可能含仓库内容，应按自己的项目数据管理。

## 开始学习和继续改造

- [项目定位、原有能力与我们的实现](docs/PROJECT.md)
- [源码学习顺序与改造任务](docs/LEARNING.md)
- [分章节学习讲义、动手练习与面试准备](docs/STUDY_GUIDE.md)
- [GitHub 接入、发布与人工反馈](docs/GITHUB_INTEGRATION.md)
- [公开仓库配置与首次云端预览](docs/GITHUB_SETUP.md)
- [真实云端预览与 Draft 事件验收](docs/CLOUD_ACCEPTANCE.md)
- [跨次云端人工反馈记忆与维护步骤](docs/CLOUD_MEMORY.md)
- [审查过程中按文件召回反馈、预算与恢复](docs/DYNAMIC_MEMORY.md)
- [PR 更新后的增量调度、检查缓存与源码学习](docs/INCREMENTAL_REVIEW.md)
- [提交可靠性与落地第一阶段记录](docs/LANDING_V1.md)
- [容器执行、自动审查部署与源码学习](docs/EXECUTION_AND_AUTOMATION.md)
- [执行隔离与自动入口的第二阶段验证](docs/LANDING_V2.md)
- [本地评估器与消融方案](docs/EVALUATION.md)
- [上下文与记忆统一设计、参考来源及落地顺序](docs/CONTEXT_MEMORY_DESIGN.md)
- [代码片段索引、摘要后的输入可见性与恢复](docs/CONTEXT_FRAGMENTS.md)
- [底座版本、来源与贡献归属](docs/ORIGIN.md)
- [首版验收记录](docs/VALIDATION.md)

```bash
uv run pytest
uv run ruff check src tests
```

统一设计 A–D 已实现，E 的四组实验入口已实现；跨仓库试点已封存并完成首轮运行，提交修复、手动 GitHub 接入、Docker 执行与自动入口已实现。独立人工审查和规划的 24 例正式评测集仍待完成。本仓库云端手动预览、PharosRAG 部署与非 Draft 自动预览均已验收，人工确认反馈的跨 job 读取也已完成两轮验收。接下来完善云端 checkpoint 恢复与跨 job 缓存，同时完成重复运行、根因复核和固定预算消融，再形成有数据支撑的简历表述。

设计参考：[Anthropic 上下文工程](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)、[长任务 Harness 的生成与评估分工](https://www.anthropic.com/engineering/harness-design-long-running-apps)、[Agent 评估](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)及 [Deep Agents Skills 文档](https://docs.langchain.com/oss/python/deepagents/skills)。这些资料提供设计思路，项目效果仍需自身数据验证。
