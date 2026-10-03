"""Write human-readable findings next to their machine-readable evidence."""

from pathlib import Path

from pr_review_harness.persistence import atomic_json


def write_report(report: dict, output: Path) -> tuple[Path, Path]:
    output.mkdir(parents=True, exist_ok=True)
    json_path, markdown_path = output / "review.json", output / "review.md"
    atomic_json(json_path, report)
    import os
    import tempfile

    fd, tmp = tempfile.mkstemp(prefix=".review-", dir=output)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(render_markdown(report))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, markdown_path)
    finally:
        Path(tmp).unlink(missing_ok=True)
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
    if (report.get("source") or {}).get("kind") == "github":
        lines.insert(2, f"PR：{report['source']['url']}\n")
    incremental = report.get("incremental", {})
    if incremental.get("enabled"):
        cache = incremental["check_cache"]
        lines.extend(
            [
                "增量调度：" + incremental["mode"] + "（" + incremental["reason"] + "）。",
                "审查范围仍为整个 PR；旧模型意见未复用。",
                f"语法检查缓存：命中 {cache['hits']} 次，未命中 {cache['misses']} 次；"
                "单元测试重新执行。",
                "",
            ]
        )
    syntax_cache = report.get("syntax_cache", {})
    if syntax_cache.get("enabled"):
        lines.extend(
            [
                f"云端语法检查缓存：命中 {syntax_cache['hits']} 次，"
                f"未命中 {syntax_cache['misses']} 次。",
                (
                    "本次恢复原审查，保留原有缓存凭据。"
                    if report.get("resumed")
                    else "缓存仅复用编译结果；本次模型审查重新执行。"
                ),
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
    execution = report.get("run_manifest", {}).get("execution", {})
    if execution.get("enabled"):
        lines.extend(["## 测试执行环境", ""])
        if execution["backend"] == "docker":
            lines.extend(
                [
                    f"- Docker 镜像：`{execution['image_id']}`（{execution['platform']}）。",
                    "- 无网络、只读根目录与代码挂载、非 root、无 API 密钥。",
                    f"- 每个版本限时 {execution['timeout']} 秒，"
                    f"内存 {execution['memory_mb']} MiB，CPU {execution['cpus']}，"
                    f"进程上限 {execution['pids']}。",
                ]
            )
        else:
            lines.append("- 本机执行，仅适用于受信任测试；没有容器隔离。")
        lines.extend(
            [
                f"- 输出仅保留末尾 {execution['output_bytes']} 字节；测试输出仍须结合代码核对。",
                "",
            ]
        )
    lines.extend(
        [
            "## 运行记录",
            "",
            f"- 上下文策略：{context.get('strategy', 'unspecified')}。",
            f"- 初始上下文：{context['used_chars']} / {context['max_chars']} 字符。",
            f"- 补充代码读取：{context['additional_read_chars']} 字符。",
            f"- 启动时仓库记忆：{context['memory_chars']} 字符。",
            f"- 耗时：{report['elapsed_seconds']} 秒。",
            "- 审查阶段可见消息 Token："
            f"{report['usage'] if report['usage']['reported'] else '模型未提供'}。",
        ]
    )
    working = context.get("working_state")
    if working:
        lines.append(
            f"- 审查状态：每轮刷新，共 {working['refreshes']} 次；"
            f"峰值 {working['peak_chars']} / {working['max_chars']} 字符。"
        )
    memory = context.get("memory_snapshot", {})
    if "records" in memory:
        ids = ", ".join(str(record["id"]) for record in memory["records"]) or "无"
        lines.append(f"- 冻结召回记忆 ID：{ids}；召回预算省略 {memory['omitted_count']} 条。")
        lines.append(f"- 记忆快照摘要：`{memory['sha256']}`。")
    pool = memory.get("candidate_pool")
    if pool is not None:
        lines.append(
            f"- 冻结候选规则：{len(pool['records'])} 条；容量限制省略 {pool['omitted_count']} 条。"
            "运行中根据实际代码读取/搜索命中召回，逐请求记录展示与预算省略。"
        )
        lines.append(f"- 候选规则版本摘要：`{pool['sha256']}`。")
    if report.get("run_id"):
        lines.append(f"- 运行 ID：`{report['run_id']}`；恢复：{report.get('resumed', False)}。")
    submission = report.get("submission")
    if submission:
        lines.append(
            f"- 结构化提交：{submission['outcome']}；"
            f"纠正请求 {submission['repair_requests']} / {submission['repair_limit']} 次。"
        )
    assembly = context.get("assembly", {})
    if assembly:
        policy = assembly["policy"]
        lines.append(
            f"- 完整请求预算：{policy['input_limit']}（{policy['mode']}）；"
            f"窗口来源：{policy['window_source']}。"
        )
        displayed = sorted(
            {i for req in assembly["requests"] for i in req.get("memory_record_ids", [])}
        )
        lines.append(f"- 实际展示给审查模型的记忆 ID：{displayed or '无'}。")
    if "budget_usage" in report:
        usage = report["budget_usage"]
        lines.append(
            f"- 累计模型尝试：{usage['model_attempts']}；"
            f"未知用量调用：{usage['unknown_usage_calls']}。"
        )
        lines.append("- 累计值包含摘要及已附加的核验；提供方内部重试用量可能不完整。")
    if context["omitted"]:
        lines.append("- 预算或格式限制导致省略：" + ", ".join(context["omitted"]))
    lines.extend(["", "完整工具轨迹和版本信息见同目录 `review.json`。", ""])
    return "\n".join(lines)


def _longest_backticks(value: str) -> int:
    import re

    return max((len(match) for match in re.findall(r"`+", value)), default=0)
