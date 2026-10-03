# 仓库代码与 Agent 文件工具的路由

## 为什么改

此前 [PharosRAG 的一轮云端预览](https://github.com/Bin-poco/PharosRAG/actions/runs/37089583454) 中，Agent 用 `ls /examples`、`glob examples/*.py`、`glob *.py` 寻找仓库文件。这些工具访问 Deep Agents 的虚拟 StateBackend，只能看到技能、记忆、笔记和卸载的结果，因此返回空结果。最终审查成功，但增加了无效导航。

原来的 `read_code` 能读代码，`search_code` 能搜索 Python 文本，缺少明确的仓库文件列表入口。改动同时补上入口、说明和错误反馈，保留一套原生文件后端与持久恢复机制。

## 工具职责

| 想做什么 | 工具 | 范围 |
| --- | --- | --- |
| 找仓库文件 | `list_code_files` | 固定 head 或 merge base 的普通 Git 文件 |
| 读精确代码 | `read_code` | 固定版本文件，带行号，单次最多 160 行 |
| 搜索 Python 文本 | `search_code` | head 的前 200 个 Python 文件，字面量，最多 12 个匹配 |
| 比较检查结果 | `run_check` | 固定双版本；云端自动流程仅语法检查 |
| 读取技能、冻结记忆、笔记或卸载结果 | `read_file` | 虚拟 Agent 存储 |
| 列出、匹配、搜索虚拟文件 | `ls`、`glob`、`grep` | 虚拟 Agent 存储；`grep` 同样是字面量搜索 |
| 保存临时笔记 | `write_file`、`edit_file`、`delete` | 原生虚拟存储；`/memories` 只读 |

`list_code_files(directory="examples", version="head", offset=0, limit=50)` 接受字面目录，不接受绝对路径或 `..`。根目录用空字符串；每页最多 100 个文件。返回 `sha`、完整 `paths`、`total`、`truncated` 和 `next_offset`。`version="base"` 指 merge base，不是更新后的目标分支 tip。

列表只取已固定的 Git tree，不读取工作区，不列入未跟踪文件，不跟随符号链接或子模块。普通非 Python 文件也能列出，再由 `read_code` 读取 UTF-8 文本。读取字符预算同时计入列表元数据；预算不足时只返回完整路径，下一页从实际返回的末尾继续。连一条完整路径也放不下时明确报错，不把未列出的文件说成不存在。

## 遇到误用会怎样

虚拟 `ls`、`read_file` 或显式指定路径的 `grep` 找不到虚拟路径时，返回 `routing="redirected"`：路径不在虚拟存储，**仓库尚未查询**，提示改用仓库工具。它不会自动执行另一个工具，也不会猜测该路径是否在仓库中。

虚拟 `ls/glob/grep` 的实际返回会带范围提示，空结果也明确只覆盖虚拟存储。合法的技能、记忆、根目录笔记和 SDK 卸载路径继续使用原生工具。这种提示不能强迫模型选择正确工具；下一轮仍可能误用，最终调用预算照常约束。

## 如何实现和学习

1. [snapshot.py](../src/pr_review_harness/snapshot.py) 的 `file_paths` 从固定 Git tree 取得可审查文件。
2. [runtime.py](../src/pr_review_harness/runtime.py) 的 `list_code_files` 处理页数、字符预算、工具轨迹；`ReviewToolScope` 调用纠正逻辑。
3. [tool_routing.py](../src/pr_review_harness/tool_routing.py) 保存原生工具的职责说明、虚拟路径检查和范围提示。
4. 使用 Deep Agents 0.7.21 公开的 `FilesystemMiddleware(system_prompt=..., custom_tool_descriptions=...)` 替换同名原生中间件。没有复制 SDK 工具实现；说明在 ContextManager 计数前已进入工具 schema。
5. 路由检查位于 `ReviewFacts` 事务内部。纠正调用也消耗预算、保存 trace 和执行回执，恢复不会重置次数或重复记录。报告的 `tool_routing` 从持久 trace 重建；原始结果保留在消息和回执中。

测试入口：[test_tool_routing.py](../tests/test_tool_routing.py)。覆盖固定版本、merge base、重命名、非代码文件、未跟踪文件、符号链接、分页与预算、错误目录纠正，以及实际图循环中的技能/记忆/笔记和中断恢复。SDK 原生摘要与卸载恢复由 [test_durable_context.py](../tests/test_durable_context.py) 继续验证。

这个改动解决工具可发现性和返回范围的问题。模型调用、token 或漏报改善需要同版本输入的重复对照实验；一轮演示不能证明质量或成本提升。

## 2026-10-03 验收

本机完整测试 **216 passed，76.32 秒**，启用 Docker 测试；Ruff、改动文件格式与 actionlint 检查通过。强制误用与中断恢复由预设模型驱动真实图循环验证，不计作模型效果数据。

已上传 Harness [`e12744e10440fbf3b3b4779687e0a0de693fbec0`](https://github.com/Bin-poco/pr-review-harness/commit/e12744e10440fbf3b3b4779687e0a0de693fbec0)，业务仓库通过 [`a0c71175e25bab1523a933deb7594f9b6ebf663e`](https://github.com/Bin-poco/PharosRAG/commit/a0c71175e25bab1523a933deb7594f9b6ebf663e) 固定使用该版本。实际检出日志及报告的实现摘要与本机源码一致。

| [PharosRAG #5 云端预览](https://github.com/Bin-poco/PharosRAG/actions/runs/37090635524) | 结果 |
| --- | --- |
| workflow / review run | `37090635524` / `35fa99a517594465a635233b9de83559` |
| 状态 / job 时间（含安装与上传） | success / 28 秒 |
| 模型 | DeepSeek 官方 `deepseek-flash`，关闭思考模式 |
| 主审查模型请求 | 4 次 |
| 合法虚拟读取 | 2 次 `read_file`，加载两个技能 |
| 仓库文件发现 | `list_code_files(directory="examples", limit=100)`，只返回演示文件；SHA 对应固定 head |
| 仓库文字搜索 | `search_code("mean_chunk_score")` |
| 虚拟仓库导航 / 误用纠正 | 本轮均为 0；云端本轮没有触发纠正分支 |
| 人工规则实际展示 | 4 / 4 次主审查请求，未截断，相同反馈 UID |
| 结果 / 独立核验 | 第 6 行 1 条 P2 / completed，supported |
| 主审查与核验累计尝试 | 模型 7 次，工具 10 次；提交修复 0 次 |
| 已报告 input / output tokens | 26,707 / 1,208 |
| 发布与 PR 状态 | preview；仍为开放 Draft，未合并；reviews、行内评论、普通评论均为 0 |

云端继续只做语法检查。源码已经在初始上下文提供，主 Agent 本轮不必再次调用 `read_code`。语法检查并未复现空输入异常，逻辑意见由源码推理产生，第二轮模型重新读取后给出支持判断。

结构化记录与五个产物的 SHA256 见 [tool-routing-20261003.json](validation/tool-routing-20261003.json)。当前启用变量为 `HARNESS_ENABLED=true`、`HARNESS_MEMORY_ENABLED=true`、`HARNESS_PUBLISH=false`。本轮表明工具列表、技能读取、人工反馈与云端链路兼容；同一个小型演示 PR 的单轮运行不能证明降低成本或提高准确率。
