# 第一版验收记录

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

改造机制已通过离线和边界测试。真实模型服务兼容性、核验的实际误判率、上下文策略的质量收益仍未测量。报告的两个阶段 Token 数仅覆盖模型返回的可见消息，内部摘要与重试未计入。
