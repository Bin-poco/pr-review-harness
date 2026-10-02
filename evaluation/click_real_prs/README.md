# Click 真实 PR 开发案例

这 3 个案例来自 [pallets/click](https://github.com/pallets/click) 的公开 PR，固定比较 merge base 与 PR head。这里没有注入缺陷。两个历史回归由后续公开 issue 和修复 PR 佐证，并在本机 Python 3.14 上用独立脚本复现。第三个案例是其中一项回归的修复 PR，标为“无**目标**缺陷”，不保证整份 PR 绝对没有其他问题。这是可公开查看标签的**开发集**，不能冒充封存测试集。

| 案例 | 公开来源 | 标注依据 | 本地复现 |
| --- | --- | --- | --- |
| 标准输入在文件参数中异常终止 | [PR #2934](https://github.com/pallets/click/pull/2934) | [issue #2939](https://github.com/pallets/click/issues/2939)、[修复 PR #2940](https://github.com/pallets/click/pull/2940) | base 正常，head 失败 |
| 分页输出关闭测试运行器的 stdout | [PR #1572](https://github.com/pallets/click/pull/1572) | [issue #3449](https://github.com/pallets/click/issues/3449)、[修复 PR #3482](https://github.com/pallets/click/pull/3482) | base 正常，head 失败 |
| 标准输入回归修复 | [PR #2940](https://github.com/pallets/click/pull/2940) | 对应 [issue #2939](https://github.com/pallets/click/issues/2939) | base 失败，head 正常；无目标缺陷 |

具体 SHA、定位和复现预期保存在 [manifest.json](manifest.json)。固定位置标签由 `prepare.py` 根据本地仓库身份和变更行生成到被 Git 忽略的 `.pr-harness/evaluation/click-real-prs/gold/`。标签只在 Agent 审查**全部结束后**交给评估器；Agent 能读取的仓库快照不含 manifest、复现脚本或后续 issue / 修复说明。

在项目根目录运行：

```bash
uv run python evaluation/click_real_prs/reproduce.py
```

命令会下载 Click 仓库、验证固定提交和 merge base、分别导出 base/head 到临时目录、执行最小复现，并生成本地 `cases.json`、`gold/*.json`、`reproduction.json`。可重复执行，不修改 Click 工作区。准备和复现不会调用模型。

完成本地 `.env` 中的 `HARNESS_API_KEY` 后，先做单案例冒烟：

```bash
set -a; source .env; set +a
uv run pr-harness benchmark \
  --cases .pr-harness/evaluation/click-real-prs/cases-smoke.json \
  --variants baseline \
  --model "$HARNESS_MODEL" --base-url "$HARNESS_BASE_URL" \
  --thinking-mode disabled \
  --out outputs/click-smoke
```

接口和输出确认正常后，再跑三例的两组对照：

```bash
uv run pr-harness benchmark \
  --cases .pr-harness/evaluation/click-real-prs/cases.json \
  --variants baseline working \
  --model "$HARNESS_MODEL" --base-url "$HARNESS_BASE_URL" \
  --thinking-mode disabled \
  --out outputs/click-real-prs
```

这里仅比较工作状态开关。没有来自更早 PR 的真实维护者反馈，所以**不运行记忆消融**；空记忆下的 memory/both 组只是重复请求。第一轮结果保存为 `benchmark.json` 和 `human-review.json`，自动位置匹配仅是候选结果。[首轮运行与本助手复核](FIRST_RUN.md)已记录逐条结论、标签修订和失败案例；`human-review.json` 是工具的复核模板文件名，当前内容并非独立人类审查者填写。两条回归的公开 issue 发生在 PR 之后；这些资料只用于标注与离线验证，不进入 Agent 上下文。
