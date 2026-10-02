"""Write human-readable findings next to their machine-readable evidence."""

import json
from pathlib import Path


def write_report(report: dict, output: Path) -> tuple[Path, Path]:
    output.mkdir(parents=True, exist_ok=True)
    json_path, markdown_path = output / "review.json", output / "review.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def render_markdown(report: dict) -> str:
    lines = ["# PR 审查报告", ""]
    if report["mode"] == "scripted-demo":
        lines.extend(
            [
                "> 此报告使用预设的工具调用示例。检查结果真实执行，结论预先给定；",
                "> 用于验证流程，不能用来评价模型审查能力。",
                "",
            ]
        )
    lines.extend(
        [
            f"- 仓库：`{report['repo']}`",
            f"- base 分支版本：`{report['base_sha']}`",
            f"- 对比起点 merge base：`{report['merge_base_sha']}`",
            f"- 审查 head：`{report['head_sha']}`",
            "- 报告绑定上述版本；提交更新后需重新审查。",
            "",
            "## 审查意见",
            "",
        ]
    )
    if not report["findings"]:
        lines.extend(["未提交可报告的缺陷；这不代表已经证明没有缺陷。", ""])
    for finding in report["findings"]:
        lines.extend(
            [
                f"### [{finding['severity']}] {finding['title']}",
                "",
                f"位置：`{finding['path']}:{finding['line']}`（head）",
                "",
                finding["explanation"],
                "",
                f"触发条件：{finding['trigger']}",
                "",
                f"证据：{', '.join(finding['evidence_ids']) or '未执行验证检查'}；"
                f"模型置信度：{finding['confidence']}（不等同于正确性）",
                "",
            ]
        )
    verification = report.get("verification")
    if verification:
        lines.extend(
            ["## 独立核验", "", "这是第二轮模型判断，供人工复核；不会自动删除审查意见。", ""]
        )
        status = verification["status"]
        if status in {"completed", "partial"}:
            for verdict in verification["verdicts"]:
                label = {
                    "supported": "支持",
                    "rejected": "驳回",
                    "uncertain": "不确定",
                }.get(verdict["verdict"], verdict["verdict"])
                lines.extend(
                    [
                        f"- 意见 {verdict['finding_index']}：**{label}**。{verdict['reason']}",
                    ]
                )
            if verification["unverified_count"]:
                lines.append(f"- 另有 {verification['unverified_count']} 条意见未核验。")
            lines.append("")
        elif status == "failed":
            lines.extend([f"核验失败：{verification['reason']}。原审查意见仍保留。", ""])
        else:
            lines.extend(["无审查意见，跳过核验。", ""])
        verifier_usage = verification.get("usage", {"reported": False})
        if status != "skipped":
            lines.extend(
                [
                    "核验阶段可见消息 Token："
                    + (str(verifier_usage) if verifier_usage["reported"] else "模型未提供"),
                    "",
                ]
            )
    lines.extend(["## 执行证据", ""])
    for evidence in report["evidence"]:
        lines.extend(
            [
                f"### {evidence['id']} · {evidence['kind']} · `{evidence['path']}`",
                "",
                f"merge base：**{evidence['base']['status']}**；"
                f"head：**{evidence['head']['status']}**。",
                "",
            ]
        )
        if evidence["same_check"] is not True:
            lines.extend(
                ["两个版本的检查文件不同；结果仅表示检查差异，不能直接归因为代码回归。", ""]
            )
        elif evidence["base"]["status"] == "passed" and evidence["head"]["status"] == "failed":
            lines.extend(["同一检查观察到版本回归；具体根因仍需结合审查意见确认。", ""])
        for version in ("base", "head"):
            output = evidence[version]["output"]
            fence = "`" * max(3, _longest_backticks(output) + 1)
            lines.extend([f"{version} 输出：", "", fence + "text", output, fence, ""])
    context = report["context"]
    lines.extend(
        [
            "## 运行记录",
            "",
            f"- 上下文策略：{context.get('strategy', 'unspecified')}。",
            f"- 初始上下文：{context['used_chars']} / {context['max_chars']} 字符。",
            f"- 补充代码读取：{context['additional_read_chars']} 字符。",
            f"- 已注入仓库记忆：{context['memory_chars']} 字符。",
            f"- 耗时：{report['elapsed_seconds']} 秒。",
            "- 审查阶段可见消息 Token："
            f"{report['usage'] if report['usage']['reported'] else '模型未提供'}。",
        ]
    )
    if context["omitted"]:
        lines.append("- 预算或格式限制导致省略：" + ", ".join(context["omitted"]))
    lines.extend(["", "完整工具轨迹和版本信息见同目录 `review.json`。", ""])
    return "\n".join(lines)


def _longest_backticks(value: str) -> int:
    import re

    return max((len(match) for match in re.findall(r"`+", value)), default=0)
