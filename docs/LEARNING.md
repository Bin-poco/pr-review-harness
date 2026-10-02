# 先运行，再顺着源码学习

## 第一次运行：看完整链路

1. 执行 `uv run pr-harness demo --verify`。
2. 打开输出目录的 `review.md`，确认 merge base 的公开测试通过、head 失败。
3. 打开 `review.json`，逐个找审查阶段的 `read_code → run_check → submit_review`，再看 `verification.trace` 中的两版读取与核验提交。
4. 记住 DemoChatModel 和 DemoVerifierModel 都预先给出了结论。切换真实模型后，它必须自己决定读哪些代码、运行哪些检查和报告什么。

## 源码顺序

| 顺序 | 本项目入口 | 底座学习入口 | 需要回答的问题 |
|---|---|---|---|
| 1 | `cli.py::_dispatch` → `runtime.py::review` | Deep Agents `graph.py::create_deep_agent` | 工具怎样注册？谁调用模型？为什么装配函数不是循环本体？ |
| 2 | `snapshot.py::Snapshot` | Git 的 merge-base、提交对象和三点 diff | 为什么读提交而不是工作区？为什么 base 不直接当对比起点？ |
| 3 | `context.py::build_context` → `read_code/search_code` | `middleware/summarization.py`、`filesystem.py` | 改动符号、调用方和测试怎样关联？字符预算与 token 有什么差别？ |
| 4 | `checks.py::CheckRunner` → `validate_findings` | LangChain 工具执行、中间件 wrap_tool_call | 检查失败是否足够？旧问题怎样识别？错误证据 ID 如何拒绝？ |
| 5 | `memory.py::MemoryStore` → memory 参数 | `middleware/memory.py::MemoryMiddleware`、`backends/state.py` | 什么真正持久？反馈由谁写？为什么驳回意见不能直接当业务规则？ |
| 6 | `skills.py`、两个 `SKILL.md` | Deep Agents `SkillsMiddleware` | 为什么系统提示只展示技能目录？全文何时读取？ |
| 7 | `verification.py::verify_report` | 第二个 `create_deep_agent` 图与工具约束 | 核验怎样独立读取两版代码？为什么核验意见仍要人工判断？ |
| 8 | `evaluation.py`、EVALUATION.md | 真实模型的 usage_metadata 与调用轨迹 | 如何证明改造有用？怎样防止把 demo 和答案泄漏当成能力？ |

学习参考源码在相邻的 `deepagents/libs/deepagents/deepagents/`。先看 graph.py 的 `create_deep_agent`、默认 backend、中间件装配和最后 `create_agent` 调用；进一步的模型 ↔ 工具循环在已安装 LangChain 的 `agents/factory.py`。

底座摘要学习关注 `compute_summarization_defaults`、`create_summarization_middleware` 和模型调用包装。小 demo 不会触发长上下文摘要；不能从 demo 成功推断摘要策略已验证。

## 接下来的自己的改造

当前已完成一版 AST 符号关联，但它仍是静态启发式。下一步先做真实模型案例分析：

- 保存同一真实 PR 的原始上下文、模型结果和人工判断。
- 比较文件名/导入策略与当前 AST 符号策略；保持模型、工具、字符/调用预算一致，观察漏报、误报和成本。
- 单独比较技能和核验阶段。核验目前只附加判断；评估器不会自动按核验意见删掉原始发现。
- 每次只改一项策略，留下代码提交、失败案例和结果对照。

再改 memory.py：实现人工反馈的撤销/修订和矛盾提示，用历史反馈测试后续 PR 的迁移效果。之后做 GitHub 接入与增量审查。

## 完成一轮学习的标准

能在面试中沿着一次真实运行解释：输入版本 → 上下文选择 → 模型与工具 → 检查证据 → 结构化意见 → 人工反馈 → 下一次记忆注入，并用代码位置和保存的轨迹回答问题。
