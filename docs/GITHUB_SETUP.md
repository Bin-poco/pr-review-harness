# GitHub 上线配置与首次云端预览

源码仓库：[Bin-poco/pr-review-harness](https://github.com/Bin-poco/pr-review-harness)，公开，默认分支 `main`。上传内容包含源码、测试、学习文档、公开评测材料、`uv.lock` 与 Actions 工作流。本机 `.env`、模型密钥、运行输出、checkpoint 和记忆数据库未上传。

本机验证包括 173 项测试通过、Docker/事件专项 23 项通过、工作流静态检查和真实 DeepSeek 事件入口预览。本仓库现已完成[云端手动预览与 Draft 跳过验收](CLOUD_ACCEPTANCE.md)，审查已启用，发布保持 `false`。以下保留首次配置步骤，供新部署复用。

## 1. 填写模型 Secret

打开仓库 [Settings → Secrets and variables → Actions](https://github.com/Bin-poco/pr-review-harness/settings/secrets/actions)，选择 **New repository secret**：

| 名称 | 内容 |
| --- | --- |
| `HARNESS_API_KEY` | 本机 `.env` 中的 DeepSeek 密钥 |

只将密钥填入 GitHub Secret。工作流读取 `${{ secrets.HARNESS_API_KEY }}`；`GITHUB_TOKEN` 由 GitHub 为每次运行提供。

## 2. 设置仓库变量

在同一设置页面的 [Variables](https://github.com/Bin-poco/pr-review-harness/settings/variables/actions) 标签添加：

| 名称 | 首次运行值 | 作用 |
| --- | --- | --- |
| `HARNESS_ENABLED` | `true` | 允许工作流执行审查 job |
| `HARNESS_PUBLISH` | `false` | 仅保存预览，不发布审查评论 |
| `HARNESS_MODEL` | `deepseek-flash`（可省略） | 已联调的默认模型 ID |
| `HARNESS_BASE_URL` | `https://api.deepseek.com`（可省略） | 默认官方接口 |

首次不要将 `HARNESS_PUBLISH` 改为 `true`。预览也会调用模型并消耗 API 用量。

## 3. 选择测试 PR

这个仓库里的工作流只审查 **pr-review-harness 本仓库的 PR**。本仓库已创建用于验收的 [Draft PR #1](https://github.com/Bin-poco/pr-review-harness/pull/1)；PharosRAG 的 `#4` 不能直接填写到这里。

准备一个本仓库可控测试 PR 后，打开 [Actions](https://github.com/Bin-poco/pr-review-harness/actions)，选择 **PR review harness → Run workflow**，分支选 `main`，填写该 PR 的编号。手动入口支持 Draft；自动 PR 事件跳过 Draft。测试 PR 保持未合并即可完成预览验收。

在成功运行页面下载 `harness-运行编号-尝试次数` artifact，查看：

- `review.md`、`review.json`：版本、意见、证据、模型与工具用量、独立核验状态。
- `publication.json`：状态应为 `preview`。
- `automation.json`：事件、目标仓库与预览结果。

如果审查或核验失败，先看失败诊断；不能把绿色页面或空意见直接视为质量达标。云端验收后另存运行链接和结论，不改写已有本机或评测历史。

## 4. 审查 PharosRAG 等业务仓库

需要把调用方工作流部署到目标业务仓库的可信默认分支，并在 **业务仓库** 配置上述 Secret/Variables。在 Harness 根目录先生成本地提案：

```bash
uv run python scripts/install_workflow.py \
  --harness-repo Bin-poco/pr-review-harness \
  --harness-sha 你已审核的40位Harness提交SHA \
  --out /你的/业务仓库/.github/workflows/pr-review.yml
```

使用已上传并审核过的完整提交 SHA；可通过 `git rev-parse HEAD` 查看本地版本。生成器拒绝分支名和覆盖已有文件，只生成本地文件。部署到 PharosRAG 默认分支属于后续步骤，目前尚未执行。

## 5. 当前运行边界

云端默认只读取固定代码并做语法检查，不执行 PR 测试或安装脚本。每个 hosted job 使用独立环境，当前不会跨 job 共享 SQLite 记忆或 checkpoint。手动 Docker 测试、容器限制和完整部署说明见 [执行与自动审查](EXECUTION_AND_AUTOMATION.md)。

文档中标记“本机”的 `outputs/...` 是历史验收路径，未上传到公开仓库。公开评测的 `FREEZE.json` 记录旧实验输入；当前源码变化后校验失败属于预期，后续实验需建立新的封存记录。
