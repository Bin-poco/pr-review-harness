# 落地第二阶段：隔离执行与自动事件入口

日期：2026-10-03。保留 [第一阶段](LANDING_V1.md)与旧评测原始记录，不覆盖历史实验。

## 实现与验收范围

| 模块 | 本次实现 | 验收方式 |
| --- | --- | --- |
| 执行配置 | local/docker、镜像 ID/平台、UID/GID、资源/时间/输出上限进入 run identity | 改变限制拒绝恢复；Docker 缺失无本机回退 |
| 容器执行 | 固定版本私有导出、无网络、只读、非 root、无密钥挂载/转发 | 真容器测试验证 UID、capabilities、no-new-privileges、挂载、网络、密钥与 cgroup 限制 |
| 超时与日志 | 宿主截止时间、容器可信 PID 1 监督；只保留输出尾部，关闭 Docker 落盘日志 | 输出洪流与无限循环终止；杀死宿主后容器自行退出；无残留容器 |
| CLI / 恢复 / 报告 | 本地与远程审查可选 Docker；远程拒绝本机；恢复用固定 ID；报告展示执行环境 | 端到端演示、完成后恢复不增加检查或调用 |
| 事件入口 | 校验仓库、数字 ID、PR 编号、base/head；旧事件与 Draft 跳过 | 伪造/过期事件不调用模型、不发布；预览与显式发送路径离线验证 |
| Actions | 可信源码与锁定依赖、固定 Action SHA、同 PR 并发取消、默认预览、artifact | 官方仓库确认版本引用；actionlint 静态检查；云端部署未启用 |
| 业务仓库调用方 | 生成检出指定公开 Harness 仓库/完整 SHA 的工作流 | 校验引用并拒绝覆盖；不自行上传或修改业务仓库 |

操作与源码路线见 [执行与自动审查指南](EXECUTION_AND_AUTOMATION.md)。

## 工程验证

- 全量：`HARNESS_TEST_DOCKER=1 uv run pytest -q --tb=short`，**173 项通过**，包括真实 Docker 测试。
- Docker 与事件专项：**23 项通过**；默认不启用 Docker 时相关集成用例会明确跳过。
- `uv run ruff check src tests evaluation/submission_replay.py scripts/install_workflow.py`、修改文件格式检查、`git diff --check`：通过。
- actionlint **1.7.12** 检查 `.github/workflows/review-pr.yml`：通过；工具来自官方发布资产，下载后核对发布校验和。
- Python 官方 `3.12-slim` 镜像完成预拉取；镜像首次拉取遇到临时服务错误，重试成功，没有更换到未知镜像源。

第一次全量后补充了导出目录私有权限处理，使用匹配的非 root UID/GID，使 umask=077 也可执行；专项覆盖该限制。当前验收数据只说明工程机制可运行，不表示模型准确率或安全隔离覆盖所有攻击。

## Docker 端到端演示

`demo --test-backend docker --verify` 完成固定版本读取、两个版本的容器测试、提交和独立核验。相同 unittest 在 merge base 通过、head 断言失败；恢复已有完成运行不增加用量的行为由真实 Docker 集成用例验证。

最终演示再次通过 CLI `resume` 恢复，恢复报告（本机 `outputs/landing-v2/docker-resumed/review.json`）与原报告的 evidence、budget_usage 完全一致。再次检查没有残留 Harness 容器；汇总见 validation.json（本机 `outputs/landing-v2/validation.json`）。

本机最终演示产物：review.md（本机 `outputs/landing-v2/docker-final/review.md`）、review.json（本机 `outputs/landing-v2/docker-final/review.json`）。演示使用预设模型结论，只证明执行链路；运行记录中的具体镜像和代码身份对应当次源码。

## DeepSeek 真实事件入口联调

使用上一阶段已授权的 [PharosRAG Draft PR #4](https://github.com/Bin-poco/PharosRAG/pull/4)，在本机生成合法 workflow_dispatch 事件，经过 `github-ci` 完成获取、审查、独立核验与发布预览：

- run ID：`5abc408297b04649819e9f54e2782c71`。
- head：`2df0e0865ce824d34b3c878656f4cb83304febb0`；base 与 merge base 仍为 `26061d62fb66cc7c89619b3c8ee6a23f11f56c2e`。
- 审查与核验累计 6 次模型、9 次工具尝试，提交修复 0 次。
- 1 条 P2 意见，定位 `examples/pr_review_harness_smoke.py:6`；独立核验状态 completed，判断 supported。
- 发布状态 **preview**，本次未发送评论。前一阶段发布仍由原远程记录保留。

本机产物：review.md（本机 `outputs/landing-v2/ci-preview/review.md`）、publication.json（本机 `outputs/landing-v2/ci-preview/publication.json`）、automation.json（本机 `outputs/landing-v2/ci-preview/automation.json`）。这是人工构造测试 PR 的入口联调，不计入审查质量评测。

## 已完成与尚未部署

**本机可运行：** GitHub 手动输入/发布、Docker 测试、版本绑定恢复、事件解析与真实模型默认预览。**已提供部署文件：** PR 事件工作流与其他业务仓库的固定源码调用方生成器。

本阶段本机验收时 Harness 没有远程地址。随后按用户授权创建公开仓库 [Bin-poco/pr-review-harness](https://github.com/Bin-poco/pr-review-harness)，上传源码、学习文档与工作流。未向 PharosRAG 默认分支提交工作流，也未配置 GitHub Secrets/Variables。没有云端 Actions 运行，不能把本机模拟事件称为线上自动运行。

下一步按 [GitHub 上线配置](GITHUB_SETUP.md) 设置模型 Secret，在可控仓库先验收云端预览；业务仓库使用固定 Harness SHA 的调用方工作流。跨 job checkpoint/人工记忆共享、增量审查、队列、分布式首次发布锁和完整运维仍未实现。自动流程仅语法检查；自定义测试依赖需要审核过的镜像。
