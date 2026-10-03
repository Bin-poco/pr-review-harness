# 版本、来源与贡献归属

## 底座

- 官方仓库：[langchain-ai/deepagents](https://github.com/langchain-ai/deepagents)，MIT。
- 本项目锁定 SDK 包：`deepagents==0.7.21`，兼容接口客户端：`langchain-openai==1.6.7`，持久扩展 `langgraph-checkpoint-sqlite==3.1.1`；完整依赖记录在 `uv.lock`。
- 学习用源码克隆：工作区相邻目录 `deepagents/`，提交 `7bc94742bb503772e207a46d29bbd7cea13e7129`，提交日期 2026-10-01。
- 克隆是源码学习参考。PyPI 包由锁文件安装；不声称该包构建内容与上述 main 提交逐文件相同。

当前作为独立业务项目使用 SDK，未修改 Deep Agents 上游源码。没有复制整套 PR-Agent 并改名。

## 原有能力与本项目代码

| 来源 | 能力 |
|---|---|
| Deep Agents | create_deep_agent、中间件装配、默认摘要与文件工具、MemoryMiddleware、SkillsMiddleware |
| LangChain/LangGraph | 模型 ↔ 工具执行循环、图运行与状态、SQLite checkpoint 与 DeltaChannel 文件/消息恢复 |
| 本项目 snapshot/context | 固定版本读取、merge base、差异行定位、受限 AST 符号上下文选择 |
| 本项目 checks/runtime/report | PR 业务工具、对照检查、意见格式/位置/证据校验、版本报告 |
| 本项目 budget/context_manager/state/persistence/review_state | 完整请求计数与 guard、事实状态事务、版本身份、执行回执、累计预算及恢复校验 |
| 本项目 submission | 预算内限次提交修复、输出诊断、修复状态 checkpoint 与失败记录 |
| 本项目 github/cli | 固定版本 PR 获取、发布预览、COMMENT/行内意见、版本与重复检查、远程反馈身份 |
| 本项目 memory | SQLite 人工反馈、仓库/路径隔离、来源、过期、修订/撤销/替换链、run/finding 来源、rule_key 主题分组与冻结召回快照 |
| 本项目 execution | 固定镜像 ID、私有快照导出、Docker 无网络/只读/非 root/资源限制、容器与宿主截止时间、有上限的管道输出；执行配置进入恢复身份 |
| 本项目 automation | GitHub 事件仓库与 SHA 校验、自动入口、可信源码工作流、默认预览与调用方工作流生成器；本仓库云端手动预览与 Draft 跳过已验收 |
| 本项目 review_skills/skills | 两个短 PR 审查 playbook 与打包接入；加载机制来自 Deep Agents |
| 本项目 verification/evaluation/benchmark | 独立核验约束、人工标注位置匹配、诊断及固定预算四组消融；未测量模型质量 |
| 本项目 demo/tests | 可重跑的流程演示与工程行为验证；不等同于模型质量数据集 |

## 业务参考

[The-PR-Agent/pr-agent](https://github.com/The-PR-Agent/pr-agent) 是 PR 审查业务参考，MIT。它已有 diff 压缩、多块处理、增量审查和审查状态等功能；未来使用其代码必须保留相应来源和许可，不能将这些已有能力全算作原创。

## 简历署名建议

写“基于 Deep Agents 设计并实现 PR 自动审查 Harness”，接着列自己的模块与真实实验结果。SDK 提供的通用摘要/记忆加载应标为底座能力；自主贡献集中在业务策略、证据链、反馈治理和评测。

上下文和记忆的其他机制参考及未采用部分见 [设计对照](CONTEXT_MEMORY_DESIGN.md)。`working_context.py` 渲染本项目的事实状态；自动历史摘要仍属于 Deep Agents。`_BudgetedSummary` 仅针对锁定 SDK 覆盖输入预算 hook，复用原生摘要实现；升级 SDK 时需重跑真实压缩集成测试。

`ReceiptStateBackend` 基于 SDK StateBackend，缓冲本步骤的文件更新并随 Command 发布，工具文件变更进入回执；避免部分 channel write 使异常步骤无法恢复。没有复制底座文件工具或另写摘要器。该兼容扩展与 `_BudgetedSummary` 均受锁定版本和集成测试约束。

GitHub 接入使用官方 REST 接口与 Git 获取对象，参数与权限参考 [GitHub Pull request reviews](https://docs.github.com/en/rest/pulls/reviews)。发布约束和回执策略为本项目实现，不声称具备分布式“恰好一次”语义。
