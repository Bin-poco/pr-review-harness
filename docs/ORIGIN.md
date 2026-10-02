# 版本、来源与贡献归属

## 底座

- 官方仓库：[langchain-ai/deepagents](https://github.com/langchain-ai/deepagents)，MIT。
- 本项目锁定 SDK 包：`deepagents==0.7.21`，兼容接口客户端：`langchain-openai==1.6.7`；完整依赖记录在 `uv.lock`。
- 学习用源码克隆：工作区相邻目录 `deepagents/`，提交 `7bc94742bb503772e207a46d29bbd7cea13e7129`，提交日期 2026-10-01。
- 克隆是源码学习参考。PyPI 包由锁文件安装；不声称该包构建内容与上述 main 提交逐文件相同。

当前作为独立业务项目使用 SDK，未修改 Deep Agents 上游源码。没有复制整套 PR-Agent 并改名。

## 原有能力与本项目代码

| 来源 | 能力 |
|---|---|
| Deep Agents | create_deep_agent、中间件装配、默认摘要与文件工具、MemoryMiddleware、SkillsMiddleware |
| LangChain/LangGraph | 模型 ↔ 工具执行循环、图运行与状态 |
| 本项目 snapshot/context | 固定版本读取、merge base、差异行定位、受限 AST 符号上下文选择 |
| 本项目 checks/runtime/report | PR 业务工具、对照检查、意见格式/位置/证据校验、版本报告 |
| 本项目 memory | SQLite 人工反馈、仓库/路径隔离、来源、过期与预算策略 |
| 本项目 review_skills/skills | 两个短 PR 审查 playbook 与打包接入；加载机制来自 Deep Agents |
| 本项目 verification/evaluation | 独立核验约束、人工标注位置匹配与诊断；未测量模型质量 |
| 本项目 demo/tests | 可重跑的流程演示与工程行为验证；不等同于模型质量数据集 |

## 业务参考

[The-PR-Agent/pr-agent](https://github.com/The-PR-Agent/pr-agent) 是 PR 审查业务参考，MIT。它已有 diff 压缩、多块处理、增量审查和审查状态等功能；未来使用其代码必须保留相应来源和许可，不能将这些已有能力全算作原创。

## 简历署名建议

写“基于 Deep Agents 设计并实现 PR 自动审查 Harness”，接着列自己的模块与真实实验结果。SDK 提供的通用摘要/记忆加载应标为底座能力；自主贡献集中在业务策略、证据链、反馈治理和评测。
