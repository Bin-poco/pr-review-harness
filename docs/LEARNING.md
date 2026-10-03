# 先运行，再顺着源码学习

详细讲义见 [PR Review Harness 学习讲义](STUDY_GUIDE.md)，包含十次学习安排、逐模块解释、可执行练习、真实失败案例和面试追问。本页保留快速阅读路线。

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
| 3 | `context_manager.py`、`budget.py` → `context.py` → 读取工具 | `middleware/summarization.py`、`filesystem.py` | 改动符号、调用方和测试怎样关联？完整请求怎样计入系统、技能、schema、记忆和历史？未知窗口怎样降级？ |
| 4 | `checks.py::CheckRunner` → `validate_findings` | LangChain 工具执行、中间件 wrap_tool_call | 检查失败是否足够？旧问题怎样识别？错误证据 ID 如何拒绝？ |
| 5 | `state.py`、`review_state.py`、`persistence.py` → `resume` | LangGraph checkpointer、DeltaChannel、`backends/state.py` | 图状态、执行回执、运行锁如何配合？中断后哪些操作可以复用？ |
| 6 | `memory.py::MemoryStore` → ContextManager | 设计中 Letta/Mem0 的机制参考 | 反馈由谁写？快照如何冻结？主题分组为什么不自动裁决？ |
| 7 | `skills.py`、两个 `SKILL.md` | Deep Agents `SkillsMiddleware` | 为什么系统提示只展示技能目录？全文何时读取？ |
| 8 | `verification.py::verify_report` | 第二个 `create_deep_agent` 图与工具约束 | 核验怎样独立读取两版代码？为什么核验意见仍要人工判断？ |
| 9 | `evaluation.py`、`benchmark.py`、EVALUATION.md | 真实模型的 usage_metadata 与调用轨迹 | 如何证明改造有用？怎样防止把 demo 和答案泄漏当成能力？ |
| 10 | `submission.py`、`tests/test_submission.py` | AgentMiddleware 的前后模型 hook 与 checkpoint | 截断/无效提交如何修复？为什么修复不新增预算，失败不能代填空意见？ |
| 11 | `github.py`、`tests/test_github.py` | GitHub REST PR/review 接口 | 获取/发布前如何固定版本？响应未知和重复发布怎样处理？ |

学习参考源码在相邻的 `deepagents/libs/deepagents/deepagents/`。先看 graph.py 的 `create_deep_agent`、默认 backend、中间件装配和最后 `create_agent` 调用；进一步的模型 ↔ 工具循环在已安装 LangChain 的 `agents/factory.py`。

底座摘要学习关注 `compute_summarization_defaults`、`create_summarization_middleware` 和模型调用包装。小 demo 通常不会触发长上下文摘要；`tests/test_durable_context.py` 的压缩集成测试会真正触发它，刻意丢失摘要细节，再检查事实和原文仍可恢复。

## 接下来的自己的改造

[统一设计](CONTEXT_MEMORY_DESIGN.md) 的 A–D 已实现，E 已有实验入口。建议跟着三条轨迹学习：

1. **正常完成**：`demo --verify --run-id learning-01`，看 `assembly.requests`、`budget_usage`、finding ID 和核验轨迹。
2. **完成后恢复**：`resume --run-id learning-01`，确认没有新增模型/工具尝试。再阅读中断、未知执行、回执重放和文件恢复测试。
3. **反馈与对照**：人工 `memory feedback` 写入来源和主题；下一 run 查看冻结快照与实际展示的记录。用 `benchmark` 跑四组配置，根因仍需人工判断。

已有 18 个跨仓库公开 PR 首轮运行、原故障重放和手动 GitHub 接入，见 [落地记录](LANDING_V1.md)。还可以沿着 [Draft 测试 PR #4](https://github.com/Bin-poco/PharosRAG/pull/4)核对报告、发布回执与行内评论；它是人工样例，仅证明链路。

执行容器与自动事件入口已实现，新增学习路线见 [执行与自动审查](EXECUTION_AND_AUTOMATION.md)。本仓库的[云端手动预览与 Draft 跳过](CLOUD_ACCEPTANCE.md)、[PharosRAG 固定源码部署及非 Draft 自动预览](PHAROS_CLOUD_ACCEPTANCE.md)均已验收。下一项工程工作是跨 job 存储与增量审查。质量验证继续保持模型、工具和预算一致，比较工作状态、记忆与上下文选择的收益及开销，并完成独立根因复核。核验只附加判断，不能直接删除原发现来制造更好的指标。

## 完成一轮学习的标准

能在面试中沿着一次真实运行解释：输入版本 → 上下文选择 → 模型与工具 → 检查证据 → 结构化意见 → 人工反馈 → 下一次记忆注入，并用代码位置和保存的轨迹回答问题。
