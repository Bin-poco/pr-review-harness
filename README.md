# PR Review Harness

面向 Python 代码仓库的 PR 自动审查学习项目。基于 **Deep Agents 0.7.21**，围绕固定版本的代码变更组织上下文、调用检查工具、生成有来源的审查意见，并复用维护者明确确认的仓库经验。

## 当前可运行版本

| 模块 | 当前实现 |
|---|---|
| Agent 运行 | 真实 Deep Agents 模型 ↔ 工具循环，限制模型调用与工具调用次数 |
| PR 输入 | 本地 Git base/head，使用 merge base 归因；读取提交内容，不受未提交修改影响 |
| 上下文 | diff、变更邻域、配置；AST 定位被改动函数/类，优先选相关调用方与测试；字符预算、截断和省略记录 |
| 证据 | Python 语法检查；可选 unittest；同一检查分别运行 merge base 和 head |
| 记忆 | SQLite 持久保存人工反馈；按仓库、路径范围、有效期筛选后注入 MemoryMiddleware |
| 审查技能 | 两个短 playbook 通过 Deep Agents Skills 按需读取，分别针对边界回归和 API 兼容 |
| 独立核验 | 可选第二轮 Agent，重新读取 base/head，对原意见给出支持、驳回或不确定的判断；保留原意见与轨迹 |
| 本地评估 | 将保存的报告与固定版本人工标注比较，输出位置命中候选指标、遗漏、重复和证据诊断 |
| 报告 | Markdown + JSON；head 行号、证据 ID、版本信息、工具轨迹、用量与耗时 |

**当前仍是本地原型。** GitHub 自动触发/行内评论、增量审查、真实模型评测集尚未实现。AST 关联是静态启发式，记忆检索采用确定性筛选；第二轮核验也只是模型意见，不能替代人工判断。

## 运行示例

需要 Python 3.11+、Git、uv。在本项目目录执行：

```bash
uv sync --group dev
uv run pr-harness demo --verify
```

示例会生成两个提交：原本正常的折扣计算和人为引入的整除错误，然后经过 **真实 Deep Agents 工具循环** 读取代码、执行两个版本的公开测试、提交审查结果并进行第二轮核验。输出目录位于 `outputs/demo-时间/`，包含代码仓库、`review.md` 和 `review.json`。

**默认 demo 的审查与核验模型都是预设工具调用器：结论预先给定，检查结果真实执行。它用于证明流程可运行，不代表 LLM 已经发现缺陷，更不是准确率评测。** 使用真实模型可执行 `uv run pr-harness demo --live --verify`。

## 接入你可用的模型

第一版使用支持工具调用的 Chat Completions 兼容接口。模型名、密钥和可选服务地址由你配置：

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

如果使用客户端默认服务，可省略 `HARNESS_BASE_URL`。支持 `OPENAI_API_KEY` 作为密钥后备变量。不会自动读取 `.env` 文件。首次真实调用需要检查服务是否支持当前工具参数格式；这次初版验收使用离线示例，未测实际服务兼容性或模型质量。

默认仅允许语法检查。添加 `--run-tests` 后允许 Agent 选择仓库中的 unittest 文件进行本地执行。检查在临时导出的提交目录运行，不修改原仓库；**临时目录不是安全沙箱**，测试会执行仓库代码。本版没有 Docker 隔离，也不会自动安装仓库依赖。

`--verify` 会额外调用一个独立核验 Agent，默认最多核验 5 条意见，最多 24 次模型调用和 32 次工具调用。可用 `--verify-max-findings`、`--verify-model-calls`、`--verify-tool-calls` 调整。核验失败会在报告中标出，原审查意见仍保留。

`--context-strategy ast` 是默认上下文策略。要在相同字符预算下对照原有文件名/导入启发式，可将它改为 `--context-strategy imports`；`context` 命令对应参数是 `--strategy`。

初始上下文默认 24,000 字符，补充读取默认最多 40,000 字符，记忆默认最多 4,000 字符。字符数不是 token 数；两个阶段的调用限制分别针对各自主循环，不含底座内部摘要/重试，因此不是精确费用上限。模型提供用量时报告分列两个阶段可见消息的 token；失败调用、内部摘要及重试可能无法统计。

## 用人工标注检查结果

将一次真实审查的人工标注保存为 `gold.json`（格式见 [评测说明](docs/EVALUATION.md)），然后运行：

```bash
uv run pr-harness evaluate \
  --report outputs/my-review/review.json \
  --gold private/gold.json \
  --out outputs/my-evaluation
```

评估结果的路径/行号匹配只是候选指标，仍要人工核对缺陷根因和触发条件。脚本 demo 即使位置全对，也不能当作模型准确率。

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
```

人工驳回的建议使用 `--disposition dismissed` 保存；它只代表此建议曾被驳回，不会自动推导出允许某种业务行为。Agent 无权把自己的结论写入持久记忆。

默认数据库为当前目录 `.pr-harness/memory.sqlite3`；跨运行请从同一项目目录执行或使用相同的 `--memory-db` 绝对路径。仓库标识基于 Git common-dir 的真实路径，同一仓库的 worktree 共享记忆，另一个克隆默认独立。记忆与审查轨迹可能含仓库内容，应按自己的项目数据管理。

## 开始学习和继续改造

- [项目定位、原有能力与我们的实现](docs/PROJECT.md)
- [源码学习顺序与改造任务](docs/LEARNING.md)
- [本地评估器与消融方案](docs/EVALUATION.md)
- [底座版本、来源与贡献归属](docs/ORIGIN.md)
- [首版验收记录](docs/VALIDATION.md)

```bash
uv run pytest
uv run ruff check src tests
```

阶段目标：真实模型样例与人工错误分析 → 固定预算下的上下文/技能/核验消融 → GitHub 接入 → 有数据支撑的简历表述。

设计参考：[Anthropic 上下文工程](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)、[长任务 Harness 的生成与评估分工](https://www.anthropic.com/engineering/harness-design-long-running-apps)、[Agent 评估](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)及 [Deep Agents Skills 文档](https://docs.langchain.com/oss/python/deepagents/skills)。这些资料提供设计思路，项目效果仍需自身数据验证。
