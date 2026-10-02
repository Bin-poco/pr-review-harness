# PR Review Harness 评测计划

## 当前状态

本项目可以读取固定版本的 `review.json` 和人工标注 JSON，执行位置匹配、重复意见统计
及行号/证据诊断。已有 `benchmark` 命令执行工作状态/记忆四组消融并生成人工复核模板。
首轮 3 个[真实 Click PR 开发案例](../evaluation/click_real_prs/README.md)已固定提交并在本地复现：2 个历史回归、1 个对应修复的无目标缺陷案例。[真实模型运行与本助手逐条复核](../evaluation/click_real_prs/FIRST_RUN.md)已完成首轮；其中一组运行失败，还有发现遗漏、误报和未定性意见。尚无独立人工复核。以下规划的 24 例正式评测集尚未构建，因此没有可靠的准确率、召回率或性能提升结论。模块单元测试只证明评估器按约定计数，不能证明审查质量提高。

跨仓库[公开试点评测集](../evaluation/independent_real_prs/README.md)现有 Flask、Requests、Werkzeug、Click 的 18 个真实 PR，双版本复现 36 次均符合预期；尚未达到规划的 24 例正式评测集规模。8 个回归与 8 个修复按根因配对，另有 2 个独立功能变更对照。[封存试跑](../evaluation/independent_real_prs/FIRST_RUN.md)完成了 36 次审查尝试，其中 33 次正常提交、3 次结构化提交失败；两种配置在共同完成的 6 个回归案例中各命中 2 个已知缺陷位置。正式统计需按根因分组报告，不能把公开标签当作私有封存集，也不能将位置匹配直接当作审查质量。

## 本地评估器

标注文件在 Agent 完成审查后才交给评估器。最小格式如下；三个版本标识须与审查报告
完全一致，`changed_lines` 从该快照的变更行范围填写，不包括 diff 中未修改的上下文行。
新生成的 `review.json` 已记录 `changed_lines`；标注文件应复制完整映射，评估器会检查两者一致。

```json
{
  "schema_version": 1,
  "repo_id": "<review.json 中的 repo_id>",
  "merge_base_sha": "<review.json 中的 merge_base_sha>",
  "head_sha": "<review.json 中的 head_sha>",
  "changed_lines": {"pricing.py": [[5, 5]]},
  "findings": [
    {
      "id": "discount-floor-division",
      "path": "pricing.py",
      "start_line": 4,
      "end_line": 6,
      "description": "20% 折扣被整除截为零，结果仍为原价"
    }
  ]
}
```

`findings` 可以为空，表示该 PR 没有目标缺陷。每个 `id` 代表一个独立根因；允许定位
范围比实际变更行稍宽，但范围内必须包含至少一条变更行，Agent 意见必须锚定其中一条
变更行。两个标注不能在同一变更行上重叠，因为只用路径和行号无法区分它们；这种案例
须人工裁定后再计分。

评估器的 Python 接口：

```python
import json
from pathlib import Path

from pr_review_harness.evaluation import evaluate_report, load_gold

report = json.loads(Path("outputs/first-run/review.json").read_text(encoding="utf-8"))
gold = load_gold("private/gold.json")
result = evaluate_report(report, gold)
print(json.dumps(result, ensure_ascii=False, indent=2))
```

命令行用法：

```bash
uv run pr-harness evaluate \
  --report outputs/my-review/review.json \
  --gold private/gold.json \
  --out outputs/my-evaluation
```

结果保存为 `outputs/my-evaluation/evaluation.json`；省略 `--out` 时打印到终端。

评估器先检查仓库、merge base 和 head 是否匹配。缺陷按变更后文件路径与行号匹配，
同一标注的多条意见只匹配一次，其余记入 `duplicate`。错误行号、未知证据 ID、绑定旧
版本的证据记入 `invalid_findings`，并计为 FP。没有任何可计分意见时 `precision=null`；
没有标注缺陷时 `recall=null`。重复意见不参与 Precision 分母，另行报告。

输出的 `metric_scope=changed_head_location_proxy` 表示**位置命中候选指标**。路径与行号
相同也可能是错误根因或不成立的触发条件。人工复核后才能把候选 TP/FP/FN 用于模型
质量结论；评估器的 `eligible_for_model_quality` 因而始终为 `false`。脚本演示即使
位置全对，也只验证工具链，不算真实模型表现。

## 评测对象与范围

第一版面向小型 Python 仓库，检查本次 PR 引入的逻辑缺陷、边界条件和接口兼容问题。
审查输出至少包含位置、触发条件、影响及证据。风格建议不计入缺陷发现指标。

当前用 3 个 Click 历史 PR 作开发案例；另建 15–20 个跨仓库真实历史 PR 候选，先
扩充并固定清单，再统一运行模型。候选需覆盖不同根因和变更规模，并加入与已知缺陷
无关的无目标缺陷 PR。回归与对应修复须按根因分组，不能当作独立缺陷；修复 PR 只
证明这一目标缺陷已修好，不能自动证明整份 PR 没有其他问题。若以后加入人工注入
缺陷，应单独成组报告，不能与真实历史 PR 混算，也不能将小样本结果解释成企业效果。

## 固定快照与变更归因

每个案例记录仓库标识、base SHA、head SHA、merge-base SHA、diff、依赖版本和运行
环境。变更归因使用 merge base 到 head 的比较，避免将 base 分支独立更新误认为 PR
新增变更。GitHub 的 PR 默认采用这种三点比较：
[GitHub 分支比较文档](https://docs.github.com/en/pull-requests/reference/branches)。

分别检查 merge base 与 head；必要时另外验证与 base 的合并结果。用于确认回归的
最小复现应在原版本正常、head 出错。只在新功能中出现且原版本不存在的接口，需由
预先定义的行为要求确认缺陷，不能简单套用“旧版本测试通过”。

旧问题可单独列出，不能计为本 PR 引入的缺陷。审查期间固定 SHA；新增提交生成
新的审查任务，不能把旧 SHA 的证据标到新代码上。

## Ground Truth 与人工复核

标注文件保存在评测侧，不提供给 Agent。输入排除已知缺陷标签、参考答案、后续修复
提交和隐藏验证测试；Agent 可以自己创建最小复现，隐藏测试只由评测器运行。

每个已知缺陷有根因描述、代码位置范围、触发条件及验证证据。人工复核所有输出：

- TP：定位到同一根因，触发条件合理，影响可以验证。
- FP：错误结论、不能成立的触发条件、旧问题或错误变更归因。
- FN：未发现已标注缺陷。
- 重复：同一根因的多条意见合并计一个发现，并单独计重复率。
- 无法判断：单列并记录原因，不交由模型自行判为正确。

如果 Agent 发现未标注的真实缺陷，人工复现后更新标注版本；所有实验配置必须按
同一标注重新计分。GitHub 既有 review 评论只能作为线索，不是完整标准答案；没有
评论不能直接标记为无缺陷。有条件时让另一位同学复核争议结果，并报告复核方式。

## 基线、消融与公平预算

| 配置 | 目的 |
| --- | --- |
| 同模型、同工具的原生 Deep Agents 基线 | 测原始能力 |
| 改进上下文策略，无经验记忆 | 测上下文贡献 |
| 改进上下文策略，加经验记忆 | 测完整方案 |
| 完整方案关闭独立核验 | 测第二轮判断的作用，同时记录额外成本 |
| 完整方案关闭按需技能 | 测短 playbook 的作用 |

固定模型版本、采样参数、提示词版本、工具权限、总输入/输出 token 上限、工具调用
次数和超时。记忆检索、摘要、子 Agent 和证据复核费用都计入总成本。每种配置使用
相同上限，记录实际消耗；更低费用和更高质量是两个独立比较维度。

当前独立核验只给出附加判断，不改变原始 `findings`；本地评估器也只给原始意见计分。
核验实验需另请人工判定其支持/驳回意见是否正确，以及它对维护者处理时间的影响，
不能直接把核验开关的评估器分数差当作误报下降。

先单次跑完整评测集，再对波动明显的案例重复三次，保留每次结果。不要只挑表现好
的一次。开发集可调策略，封存集结果出来后不能继续调策略再宣称同一轮泛化结果。

## 跨 PR 记忆评测与无泄漏

记忆的当前实现路线是 **SQLite 持久存储 → 仓库、文件范围和有效期筛选 → 生成有
字符预算的冻结快照 → ContextManager 在摘要前注入**。SQLite 跨进程保存反馈；
StateBackend 文件随 LangGraph SQLite checkpoint 恢复，生命周期属于当前 run；长期
反馈库属于跨 PR 数据，两者分开。

只有人工 CLI 反馈可以写入记忆，模型发现不会自动成为已确认经验。`accepted` 记录
为有范围的规则或经验；`dismissed` 只提醒此前建议被驳回，不能推断业务允许某种行为，
也不能把单次驳回变成所有未来 PR 的豁免。每条记录保留来源、时间、范围和有效期。

另建两条小型“较早 PR → 后续 PR”序列：

1. 只从较早 PR 的明确维护者反馈学习约定和经验。
2. 用不同后续 PR 检查经验能否迁移，既包含应报告问题，也包含不应报告的问题。
3. 测试 PR 的标签、修复答案和隐藏测试不能写入记忆。
4. 不同仓库隔离；每个配置从相同初始记忆快照开始。
5. 独立测试案例之间重置记忆；顺序学习实验单独报告，不混入独立案例结果。
6. 比较记忆开关，并检查过期、错误或驳回反馈是否增加误报。

目前记忆采用确定性范围与期限筛选，不包含向量检索、自动冲突解决或自动经验学习。
后续新增这些能力时，应增加对应消融，避免把其他模块效果归因给记忆。

## 指标与报告

- 缺陷级 Precision = TP / (TP + FP)。没有任何发现时记为未定义，并报告零发现。
- 缺陷级 Recall = TP / (TP + FN)。没有标注缺陷的子集不计算召回率。
- 无目标缺陷 PR 误报率 = 至少出现一条 FP 的无目标缺陷 PR 数 / 此类 PR 总数。
- 重复意见率 = 被去重的意见数 / 原始意见总数。
- 耗时、总 token、实际费用、工具调用数和执行失败率。

报告实际 TP/FP/FN 数量、数据划分、标注版本、模型与配置、预算、失败原因及完整
执行轨迹。执行失败样本必须保留，明确全量结果与成功执行子集结果。

自动审查耗时不能直接推导人工维护者响应速度或工作量下降。简历可以先写已实现的
机制与可复现流程；评测完成后再填写真实数字，不预设“提升 X%”。


## 已实现的四组消融命令

创建 `cases.json`，路径相对该文件解析；base/head 在运行开始固定为 SHA：

```json
{
  "schema_version": 1,
  "cases": [
    {
      "id": "case-01",
      "repo": "../your-repo",
      "base": "main",
      "head": "feature",
      "gold": "gold-01.json",
      "strategy": "ast",
      "run_tests": false,
      "memory_db": "prior-feedback.sqlite3",
      "memory_is_prior_feedback": true
    }
  ]
}
```

没有历史反馈时省略两个 memory 字段。记忆库必须来自较早 PR 的明确人工反馈，不能写入当前目标缺陷的答案。声明 true 只是数据契约，无法自动证明无泄漏。

```bash
uv run pr-harness benchmark --cases private/cases.json --out outputs/ablation \
  --window-tokens 65536 --total-model-calls 64
```

默认运行 baseline（两者关）、working（仅工作状态）、memory（仅记忆）、both（两者开）。同一 case 的记忆快照只捕获一次，主工具/模型/预算一致；工作状态开关是唯一的 policy 变化。`--variants` 可选择子集；`--scripted` 只用于折扣 demo 的流程验收。

每组保存报告或失败记录；所有 Agent 运行结束后才读取 gold，输出 `benchmark.json` 的位置候选计数、SHA、配置、用量和耗时，以及空白 `human-review.json`。人工逐项填写根因是否匹配、是否误报及理由。遗漏从 evaluation.json 的 unmatched 标签另行复核。eligible_for_model_quality 保持 false，脚本不会凭位置计数生成质量结论。

如果要比较 AST/imports、技能或核验，分别建立实验，不把这些变化混入本命令的四组结论。真实模型具有随机性；当前入口每组一次，重复实验需要保留全部 batch，不能只选较好的结果。
