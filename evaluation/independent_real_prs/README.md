# 跨仓库真实 PR 评测候选集

这是 Click 开发案例之外的**跨仓库公开评测集**。当前 18 个 PR 来自 Flask、Requests、Werkzeug、Click 四个上游仓库，包含 8 个引入回归的 PR、对应的 8 个修复 PR，以及 2 个与这些回归无关的功能变更对照 PR。修复与对照 PR 只标为“无已知目标缺陷”，不声称整份 PR 绝对无错。本轮用于固定案例的试点评测，不当作私有封存集或无泄漏泛化结果。

| 仓库 | 引入回归 | 修复 | 已验证的触发条件 |
| --- | --- | --- | --- |
| Flask | [#5157](https://github.com/pallets/flask/pull/5157)，[报告 #5160](https://github.com/pallets/flask/issues/5160) | [#5161](https://github.com/pallets/flask/pull/5161) | 脚本的 `__main__.__spec__` 为空时初始化 Flask |
| Requests | [#5681](https://github.com/psf/requests/pull/5681)，[报告 #5888](https://github.com/psf/requests/issues/5888) | [#5924](https://github.com/psf/requests/pull/5924) | 初次发送请求时保留手动设置的 `Proxy-Authorization` |
| Requests | [#7272](https://github.com/psf/requests/pull/7272)，[报告 #7432](https://github.com/psf/requests/issues/7432) | [#7433](https://github.com/psf/requests/pull/7433) | 通过 `__getattr__` 代理文件接口的上传流应被识别为可回退的流 |
| Werkzeug | [#3081](https://github.com/pallets/werkzeug/pull/3081)，[报告 #3088](https://github.com/pallets/werkzeug/issues/3088) | [#3089](https://github.com/pallets/werkzeug/pull/3089) | 64 KiB 边界附近的 multipart 文件字段后，文本字段不应混入 CRLF |
| Werkzeug | [#3113](https://github.com/pallets/werkzeug/pull/3113)，[报告 #3142](https://github.com/pallets/werkzeug/issues/3142) | [#3148](https://github.com/pallets/werkzeug/pull/3148) | 未配置可信 Host 时，允许旧版 HTTP 请求缺省 Host |
| Werkzeug | [#2974](https://github.com/pallets/werkzeug/pull/2974)，[报告 #2985](https://github.com/pallets/werkzeug/issues/2985) | [#2986](https://github.com/pallets/werkzeug/pull/2986) | `EnvironHeaders` 转字符串时保留环境中的请求头 |
| Click | [#1825](https://github.com/pallets/click/pull/1825)，[报告 #1921](https://github.com/pallets/click/issues/1921) | [#2006](https://github.com/pallets/click/pull/2006) | 从其他工作目录解析相对符号链接 |
| Click | [#3030](https://github.com/pallets/click/pull/3030)，[报告 #3145](https://github.com/pallets/click/issues/3145) | [#3224](https://github.com/pallets/click/pull/3224) | 查询不存在的默认值返回 `None`，不泄露内部哨兵值 |

独立对照：[Click #2818](https://github.com/pallets/click/pull/2818) 增加 `CliRunner` 级别的异常传播配置；[Werkzeug #2902](https://github.com/pallets/werkzeug/pull/2902) 增加 HTTP 421 异常映射。这两项的预期功能都有 base 失败、head 通过的复现；这一结果仅验证目标功能，不能证明变更的所有路径均无缺陷。

固定 SHA、预期结果、根因分组和评测侧标签见 [manifest.json](manifest.json)；十个不联网的最小复现脚本在 `reproducers/`。代码库只作为 Agent 的快照输入，标注、issue 和修复说明不写入代码库。Requests #5681 和 #7272、Werkzeug #3113 的 `base_sha` 与 `merge_base_sha` 不同，评测以实际 merge base 为准。

在项目根目录执行：

```bash
uv run python evaluation/independent_real_prs/reproduce.py
```

首次运行会在被 Git 忽略的 `.pr-harness/evaluation/independent-real-prs/` 准备四个上游仓库的本地快照（Requests 的两个历史版本分别准备），创建固定 Python 3.12.13 与依赖版本的环境，导出每个 PR 的 merge base/head，确认导入的是该版本的源码，并运行 36 次离线复现。运行结果写入同目录的 `reproduction.json`；只要一个结果不符合 manifest，命令就失败。单独运行 `prepare.py` 可只准备快照和评测文件，不执行复现。生成的 `cases.json` 可交给 `pr-harness benchmark`。

本轮输入及配置的 SHA-256 摘要记录在 [FREEZE.json](FREEZE.json)。运行前执行 `uv run python evaluation/independent_real_prs/seal.py --verify`；任何源文件或复现脚本变更都会使校验失败。模型配置固定为 DeepSeek 官方 `deepseek-flash`、关闭思考、温度 0；每个 PR 跑 `baseline` 和 `working` 各一次，主审查最多 12 次模型调用、24 次工具调用、读取 40,000 字符。[首轮结果与失败分析](FIRST_RUN.md)已记录。完整模型输出保存在本机 `outputs/independent-real-prs/`，不会写入本目录。

当前限制：修复 PR 与对应回归共享根因，16 个成对案例只能按 8 组独立缺陷理解；2 个对照仅有目标功能验证，没有“无任何缺陷”的证明。所有案例标签都来自公开资料，模型可能已有相关知识。位置匹配只是候选指标，所有模型输出需独立人类审查者核对根因、触发条件和误报。后续修改标签必须增加 `label_revision`，不能静默覆盖既有结论；已封存的试点评测不能原地覆盖。
